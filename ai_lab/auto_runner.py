# -*- coding: utf-8 -*-
"""
auto_runner.py — 全自动跑局（AoE2Control 驱动）
================================================
无人值守地跑完一整代（或指定数量）的对局：

  python auto_runner.py              # 跑完当前代的全部赛程
  python auto_runner.py --matches 4  # 只跑 4 场（调试用）

原理：
  1. 游戏内由 AoE2Control 的 evolab_driver 模块驱动（挂在玩家1）：
     自动建房 → P1 自毁成观察者 → 结束后自动开下一局，循环不息。
  2. 对局双方 AI 固定为游戏目录里的 EvoAI_A / EvoAI_B（大厅里选一次即可），
     本脚本在每局间隙改写这两个 .per 文件的内容来切换对战组合——
     槽位名不变，基因内容在变。
  3. 每局结束的 .aoe2record 录像用 mgz 解析出胜者，写进 evolve 的积分账本。

前置条件（一次性，详见 README「全自动模式」）：
  - 大厅建过一次房：P2=EvoAI_A、P3=EvoAI_B，之后全自动沿用该配置
  - CONTROL 菜单里把 evolab_driver 指派给 Player 1（只做一次，settings.ini 会记住）
  - 关闭 Multithreading / Tournament Mode（否则 Dispatch* 被拒绝）
"""

import argparse
import json
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).parent))
import genome as G  # noqa: E402
import make_ai as MK  # noqa: E402
import report as RPT  # noqa: E402
import evolve as EV  # noqa: E402

LAB = ROOT / "lab_data"
CONTROL_CFG = Path.home().parent.parent / "AppData/Roaming"  # %APPDATA%
APPDATA = Path.home().drive and Path(str(Path.home()).split("Users")[0] + "Users")  # unused fallback


def appdata():
    import os
    return Path(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming")))


def load_config():
    with open(ROOT / "config.json", "r", encoding="utf-8") as f:
        return json.load(f)


# ------------------------------------------------------------------
# 部署：CONTROL 启动器 + evolab_driver 模块
# ------------------------------------------------------------------
def deploy_module(cfg):
    src = Path(__file__).parent / "control" / "evolab_driver"
    dst = appdata() / "CONTROL" / "AoE2Control" / "modules" / "evolab_driver"
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src / "evolab_driver.main.lua", dst / "evolab_driver.main.lua")
    print("[部署] 驱动模块 → %s" % dst)
    return dst


def check_module_assigned():
    """settings.ini 里应能找到模块名（说明已指派给某个玩家槽位）。"""
    ini = appdata() / "CONTROL" / "AoE2Control" / "settings.ini"
    if not ini.exists():
        return False, "settings.ini 还不存在（CONTROL 首次启动后才会生成）"
    try:
        text = ini.read_text(encoding="utf-8", errors="replace")
    except Exception:
        text = ini.read_text(encoding="latin-1", errors="replace")
    if "evolab_driver" in text:
        return True, ""
    return False, "settings.ini 里没找到 evolab_driver（模块还没指派给玩家1）"


# ------------------------------------------------------------------
# 游戏与 CONTROL 生命周期
# ------------------------------------------------------------------
def game_running():
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq AoE2DE_s.exe", "/NH"],
            capture_output=True, text=True, timeout=15)
        return "AoE2DE_s.exe" in out.stdout
    except Exception:
        return False


def start_game(cfg):
    exe = Path(cfg["game"]["install_dir"]) / cfg["game"]["exe"]
    print("[启动] 游戏主程序: %s" % exe)
    subprocess.Popen([str(exe)], cwd=str(exe.parent))


def ensure_control(cfg, max_wait_s=600):
    """确保游戏运行且 CONTROL 就绪。返回 True/False。"""
    launcher = Path(cfg["control"]["launcher"])
    if not launcher.is_absolute():
        launcher = ROOT / launcher
    launcher = launcher.resolve()
    if not launcher.exists():
        print("[错误] 找不到 AoE2Control 启动器: %s" % launcher)
        return False

    if not game_running():
        start_game(cfg)
        print("[等待] 游戏启动中（首次启动可能需要 1~2 分钟）...")

    deadline = time.time() + max_wait_s
    while time.time() < deadline:
        if not game_running():
            time.sleep(10)
            continue
        proc = subprocess.run(
            [str(launcher), "--headless", "--timeout-ms", "120000"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=180)
        out = proc.stdout or ""
        last = [l for l in out.splitlines() if l.strip()]
        print("[CONTROL] %s (exit=%d)" % (last[-1] if last else "无输出", proc.returncode))
        if proc.returncode == 0:
            return True
        if proc.returncode in (6, 7):
            time.sleep(15)  # 游戏还没起来/窗口未就绪，继续等
            continue
        print("[错误] CONTROL 启动失败，输出：\n%s" % out)
        return False
    print("[错误] 等待游戏/CONTROL 超时")
    return False


# ------------------------------------------------------------------
# 对局结果
# ------------------------------------------------------------------
def list_recordings(cfg):
    rec_dir = Path(cfg["game"]["recordings_dir"])
    dirs = [rec_dir, rec_dir.parent]  # 单人录像有时落在 savegame 根目录
    seen = {}
    for d in dirs:
        if d.exists():
            for p in d.glob("*.aoe2record"):
                seen[str(p)] = p
    return seen


def wait_new_recording(cfg, before, timeout_min):
    deadline = time.time() + timeout_min * 60
    while time.time() < deadline:
        now = list_recordings(cfg)
        new = [p for k, p in now.items() if k not in before]
        if new:
            newest = max(new, key=lambda p: p.stat().st_mtime)
            time.sleep(3)  # 等文件写完
            return newest
        time.sleep(5)
    return None


def resolve_winner(info, gen, a, b):
    """返回胜者个体名 'EvoAI_G{gen}P{a|b}' 或 None。
    玩家槽位约定：P1=观察者（索引0，恒输），P2=EvoAI_A=个体a（索引1），P3=EvoAI_B=个体b（索引2）。
    mgz 的 players 列表顺序即玩家号顺序。"""
    names = {1: "EvoAI_G%dP%d" % (gen, a), 2: "EvoAI_G%dP%d" % (gen, b)}
    players = info.get("players", [])
    # 1) mgz postgame 的 winner 标记
    for i, p in enumerate(players):
        if p.get("winner") and i in names:
            return names[i]
    # 2) 比分定胜负
    ai_players = [players[i] for i in (1, 2) if i < len(players)]
    if len(ai_players) == 2:
        s1, s2 = ai_players[0].get("score"), ai_players[1].get("score")
        if isinstance(s1, (int, float)) and isinstance(s2, (int, float)) and s1 != s2:
            idx = 1 if s1 > s2 else 2
            return names[idx]
    return None


# ------------------------------------------------------------------
# 主流程
# ------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="EvoLab 全自动跑局")
    ap.add_argument("--matches", type=int, default=None, help="最多跑几场（默认跑完赛程）")
    ap.add_argument("--gen", type=int, default=None, help="指定代数（默认当前最新代）")
    args = ap.parse_args()

    cfg = load_config()
    deploy_module(cfg)

    # ---- 进化赛程 ----
    gen = args.gen if args.gen is not None else EV.current_gen()
    gen_file = LAB / "generations" / ("gen_%d.json" % gen)
    if not gen_file.exists():
        sys.exit("没有第 %d 代种群，先运行 evolve.py init" % gen)
    gene_pop = json.loads(gen_file.read_text(encoding="utf-8"))
    pop = len(gene_pop)
    ec = EV.evo_cfg(cfg)
    rng = random.Random(gen * 7919)
    schedule = EV.make_schedule(pop, int(ec.get("matches_per_ai", 3)), rng)
    if args.matches:
        schedule = schedule[: args.matches]

    res_dir = LAB / "results"
    res_dir.mkdir(parents=True, exist_ok=True)
    res_file = res_dir / ("gen_%d.json" % gen)
    results = json.loads(res_file.read_text(encoding="utf-8")) if res_file.exists() else []
    done = len(results)
    schedule = schedule[done:]
    print("[赛程] 第 %d 代共需 %d 场，已完成 %d 场，本次跑 %d 场"
          % (gen, done + len(schedule), done, len(schedule)))
    if not schedule:
        print("[完成] 本代赛程已全部跑完，直接执行 evolve.py next 即可")
        return

    # ---- 游戏与 CONTROL ----
    if not ensure_control(cfg):
        sys.exit(1)
    ok, why = check_module_assigned()
    if not ok:
        print()
        print("=" * 60)
        print("  还差一步（只做一次）：把 evolab_driver 指派给玩家 1")
        print("=" * 60)
        print("  1. 切到游戏窗口，按 Shift 打开 CONTROL 菜单")
        print("  2. 展开 MODULES → Player 1")
        print("  3. Module 下拉框选 evolab_driver，保持 Enabled 打开")
        print("  4. 确认 Multithreading / Tournament Mode 处于关闭")
        print("  然后重新运行本命令即可。")
        print("  （%s）" % why)
        print("=" * 60)
        sys.exit(2)

    print()
    print("[开始] 全自动跑局。停止方法：CONTROL 菜单关掉 Player1 的模块，或按 Delete 卸载 CONTROL。")
    print("[提示] 想看就切到游戏窗口；不想看就让它跑，战报自动落盘。")
    print()

    timeout_min = int(cfg.get("control", {}).get("auto_match_timeout_min", 150))
    for idx, (a, b) in enumerate(schedule):
        print("[对局 %d/%d] EvoAI_G%dP%d  vs  EvoAI_G%dP%d"
              % (done + idx + 1, done + len(schedule), gen, a, gen, b))
        # 换上本局的两个基因
        MK.install("A", {k: v for k, v in gene_pop[a].items()}, cfg)
        MK.install("B", {k: v for k, v in gene_pop[b].items()}, cfg)

        before = list_recordings(cfg)
        rec = wait_new_recording(cfg, before, timeout_min)
        if rec is None:
            print("[超时] %d 分钟没有等到新录像，可能卡局。已暂停。" % timeout_min)
            print("       排查后重跑本命令会自动续上进度。")
            break

        print("[录像] %s" % rec.name)
        try:
            info = RPT.parse_record(rec)
        except SystemExit:
            raise
        except Exception as e:
            info = {"players": [], "winners": []}
            print("[警告] 录像解析失败: %s" % e)

        winner_name = resolve_winner(info, gen, a, b)
        na, nb = "EvoAI_G%dP%d" % (gen, a), "EvoAI_G%dP%d" % (gen, b)
        scores = {}
        players = info.get("players", [])
        for i, p in enumerate(players):
            if i == 1:
                scores[na] = p.get("score")
            elif i == 2:
                scores[nb] = p.get("score")
        results.append({
            "players": [[na, winner_name == na], [nb, winner_name == nb]],
            "winners": [winner_name] if winner_name else [],
            "scores": scores,
            "duration_min": info.get("duration_min"),
            "record": str(rec),
        })
        res_file.write_text(json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8")
        if winner_name:
            print("[结果] 胜者: %s" % winner_name)
        else:
            print("[结果] 未能自动判定胜者，积分时按无胜者处理（可人工核对录像）")
    else:
        print()
        print("[完成] 本次赛程全部跑完！")
        print("[下一步] python evolve.py next   # 汇总积分，进化出下一代")
        print("[停止]  记得在游戏里按 Shift → 关掉 Player1 的模块（或按 Delete 卸载 CONTROL）")


if __name__ == "__main__":
    main()
