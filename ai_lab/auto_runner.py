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
import csv
import io
import json
import math
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


WINDOW_MIN_WIDTH = 640
WINDOW_MIN_HEIGHT = 360


def game_window_status():
    """Return (ready, reason) for a visible, restored AoE2 game window."""
    if sys.platform != "win32":
        return False, "窗口状态探测仅支持 Windows"
    try:
        import ctypes
        from ctypes import wintypes

        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq AoE2DE_s.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=15)
        pids = set()
        for row in csv.reader(io.StringIO(out.stdout or "")):
            if len(row) >= 2 and row[0].lower() == "aoe2de_s.exe":
                try:
                    pids.add(int(row[1]))
                except ValueError:
                    pass
        if not pids:
            return False, "未找到 AoE2DE_s.exe 进程"

        user32 = ctypes.windll.user32
        found = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        @callback_type
        def visit(hwnd, _):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in pids and user32.IsWindowVisible(hwnd):
                rect = wintypes.RECT()
                if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                    minimized = bool(user32.IsIconic(hwnd))
                    width, height = rect.right - rect.left, rect.bottom - rect.top
                    found.append((minimized, width, height))
            return True

        user32.EnumWindows(visit, 0)
        if not found:
            return False, "游戏窗口不存在或不可见"
        if not any(not minimized for minimized, _, _ in found):
            return False, "游戏窗口已最小化"
        if not any(not minimized and width >= WINDOW_MIN_WIDTH and height >= WINDOW_MIN_HEIGHT
                   for minimized, width, height in found):
            return False, "游戏窗口尺寸小于 640×360"
        return True, "窗口可见、未最小化且尺寸满足要求"
    except Exception as exc:
        return False, "窗口探测失败: %s" % exc


def wait_for_game_window(timeout_s, window_probe=game_window_status,
                         poll_interval_s=2, monotonic=time.monotonic, sleep=time.sleep):
    deadline = monotonic() + timeout_s
    last_reason = "尚未探测"
    while True:
        ready, last_reason = window_probe()
        if ready:
            return True, last_reason
        remaining = deadline - monotonic()
        if remaining <= 0:
            return False, "等待游戏窗口就绪超时（%s）" % last_reason
        sleep(min(poll_interval_s, remaining))


def ensure_control(cfg, max_wait_s=600, window_probe=game_window_status,
                   control_runner=subprocess.run, monotonic=time.monotonic, sleep=time.sleep):
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

    deadline = monotonic() + max_wait_s
    last_window_reason = "未检测"
    while monotonic() < deadline:
        if not game_running():
            sleep(min(10, max(0, deadline - monotonic())))
            continue
        ready, last_window_reason = window_probe()
        if not ready:
            sleep(min(2, max(0, deadline - monotonic())))
            continue
        proc = control_runner(
            [str(launcher), "--headless", "--timeout-ms", "120000"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=180)
        out = proc.stdout or ""
        last = [l for l in out.splitlines() if l.strip()]
        print("[CONTROL] %s (exit=%d)" % (last[-1] if last else "无输出", proc.returncode))
        if proc.returncode == 0:
            return True
        if proc.returncode in (6, 7):
            last_window_reason = "Headless exit %d: %s" % (
                proc.returncode, last[-1] if last else "无输出")
            sleep(min(2, max(0, deadline - monotonic())))
            continue
        print("[错误] CONTROL 启动失败，输出：\n%s" % out)
        return False
    print("[错误] 等待游戏窗口/CONTROL 超时（窗口状态：%s）" % last_window_reason)
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


def _players_by_identity(info):
    """Index the two replay AIs by their stable installed slot names."""
    players_by_identity = {}
    for player in info.get("players", []):
        name = str(player.get("name", ""))
        identity = next((key for key in ("EvoAI_A", "EvoAI_B") if key in name), None)
        if identity:
            if identity in players_by_identity:
                return None
            players_by_identity[identity] = player
    if set(players_by_identity) != {"EvoAI_A", "EvoAI_B"}:
        return None
    return players_by_identity


def _valid_score(value):
    return value is None or (
        isinstance(value, (int, float)) and not isinstance(value, bool)
        and (isinstance(value, int) or math.isfinite(value))
    )


def resolve_winner(info, gen, a, b):
    """Map one unambiguous replay winner to its installed genome identity."""
    identity_to_genome = {
        "EvoAI_A": "EvoAI_G%dP%d" % (gen, a),
        "EvoAI_B": "EvoAI_G%dP%d" % (gen, b),
    }
    players_by_identity = _players_by_identity(info)
    if players_by_identity is None:
        return None

    if any(not _valid_score(player.get("score")) for player in players_by_identity.values()):
        return None
    explicit_winners = [identity for identity, player in players_by_identity.items()
                        if player.get("winner") is True]
    if len(explicit_winners) > 1:
        return None
    if explicit_winners:
        return identity_to_genome[explicit_winners[0]]

    # If the replay has no explicit winner marker, compare scores by identity.
    score_a = players_by_identity["EvoAI_A"].get("score")
    score_b = players_by_identity["EvoAI_B"].get("score")
    if (isinstance(score_a, (int, float)) and isinstance(score_b, (int, float))
            and score_a != score_b):
        identity = "EvoAI_A" if score_a > score_b else "EvoAI_B"
        return identity_to_genome[identity]
    return None


def match_result_from_replay(info, gen, a, b, match, record, record_id):
    """Build one ledger row, rejecting ambiguous winners and invalid scores."""
    na, nb = "EvoAI_G%dP%d" % (gen, a), "EvoAI_G%dP%d" % (gen, b)
    players_by_identity = _players_by_identity(info)
    if players_by_identity is None:
        raise ValueError("录像未能唯一映射到 EvoAI_A 与 EvoAI_B")
    if any(not _valid_score(player.get("score"))
           for player in players_by_identity.values()):
        raise ValueError("录像比分必须是有限数值或缺失")
    winner_name = resolve_winner(info, gen, a, b)
    if winner_name not in (na, nb):
        raise ValueError("录像胜者无效、多个胜者或无法唯一判定")
    scores = {
        na: players_by_identity["EvoAI_A"].get("score"),
        nb: players_by_identity["EvoAI_B"].get("score"),
    }
    return {
        "match_id": match["match_id"],
        "players": [[na, winner_name == na], [nb, winner_name == nb]],
        "winners": [winner_name],
        "scores": scores,
        "duration_min": info.get("duration_min"),
        "record": str(record),
        "record_id": record_id,
    }


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
    pairs = EV.make_schedule(pop, int(ec.get("matches_per_ai", 3)), random.Random(gen * 7919))
    expected_count = pop * int(ec.get("matches_per_ai", 3)) // 2
    res_dir = LAB / "results"
    res_dir.mkdir(parents=True, exist_ok=True)
    res_file = res_dir / ("gen_%d.json" % gen)
    results = json.loads(res_file.read_text(encoding="utf-8")) if res_file.exists() else []
    manifest = EV.ER.load_schedule(LAB, gen, pairs, results, pop, expected_count)
    try:
        EV.ER.validate_schedule(manifest, pop, expected_count)
    except ValueError as e:
        sys.exit("[拒绝] 赛程本身不完整或覆盖不足：%s" % e)

    try:
        accepted = EV.ER.reconcile_results(results, manifest)
    except ValueError as e:
        sys.exit("[拒绝] 现有结果账本无效，未启动对局：%s" % e)
    # Persist assigned IDs for legacy ledger rows before resuming.
    EV.ER.write_results(res_file, results)
    done = len(accepted)
    schedule = EV.ER.pending_matches(results, manifest)
    if args.matches is not None:
        schedule = schedule[:max(0, args.matches)]
    print("[赛程] 第 %d 代计划 %d 场，已完成 %d 场，本次跑 %d 场"
          % (gen, len(manifest["matches"]), done, len(schedule)))
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
    for idx, match in enumerate(schedule):
        a, b = [EV.ER.individual_from_name(n, gen, pop) for n in match["players"]]
        if a is None or b is None:
            sys.exit("[拒绝] 赛程参赛名无效: %s" % match)
        print("[对局 %d/%d] EvoAI_G%dP%d  vs  EvoAI_G%dP%d"
              % (done + idx + 1, len(manifest["matches"]), gen, a, gen, b))
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

        na, nb = "EvoAI_G%dP%d" % (gen, a), "EvoAI_G%dP%d" % (gen, b)
        try:
            result = match_result_from_replay(
                info, gen, a, b, match, rec.resolve(), EV.ER.match_id_for_recording(rec))
        except ValueError as e:
            print("[拒绝] %s；录像保留，账本不推进。修正录像结果后重新运行。" % e)
            break
        winner_name = result["winners"][0]
        results.append(result)
        try:
            EV.ER.reconcile_results(results, manifest)
        except ValueError as e:
            results.pop()
            print("[拒绝] 对局结果未写入账本：%s" % e)
            break
        EV.ER.write_results(res_file, results)
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
