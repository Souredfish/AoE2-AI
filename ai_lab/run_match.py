# -*- coding: utf-8 -*-
"""
run_match.py — 启动一场 AI 对战（观战模式）
============================================
两种用法：

1) AI 对 AI（进化对局）：
   python run_match.py EvoAI_A EvoAI_B
   → 自动安装/刷新两个 AI，启动游戏，你以观战者身份进房，
     打完后回到终端按回车，脚本自动抓取录像生成战报。

2) 你的 AI 替你打电脑：
   python run_match.py EvoAI_Champion --vs-cpu hardest
   → 启动游戏后建房间，1 号位选 EvoAI_Champion，2 号位选电脑，
     你可以挂机观战，也可以直接等结果。

对局建议（重要）：
  - 难度固定 Hard 或以上（进化参数走 hard 分支）
  - 地图固定 Arabia（或你在 config.json 里改）
  - 速度可开"快速"加速迭代
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).parent))
import genome as G  # noqa: E402
import make_ai as MK  # noqa: E402
import recordings as REC  # noqa: E402


def load_config():
    with open(ROOT / "config.json", "r", encoding="utf-8") as f:
        return json.load(f)


def snapshot_latest_record(cfg):
    return REC.latest_recording(cfg)


def launch_game(cfg):
    exe = Path(cfg["game"]["install_dir"]) / cfg["game"]["exe"]
    if not exe.exists():
        sys.exit("找不到游戏主程序: %s" % exe)
    print("[启动] %s" % exe)
    subprocess.Popen([str(exe)], cwd=str(exe.parent))


def print_lobby_checklist(cfg, ais, vs_cpu=None):
    m = cfg["match"]
    print()
    print("=" * 56)
    print("  游戏已启动，请按以下步骤建房（1 分钟搞定）：")
    print("=" * 56)
    print("  1. 主菜单 → 单人游戏 → 创建随机地图对局")
    print("  2. 地图: %s    速度: %s    人口: %s" % (m["map"], m["game_speed"], m["pop_cap"]))
    print("  3. 难度: %s   （必须！进化参数按难度分支生效）" % m["difficulty"])
    if vs_cpu:
        print("  4. 1号位(你): 设为 观战/电脑托管 → 选 %s" % ais[0])
        print("     2号位: 电脑 → 难度 %s" % vs_cpu)
        print("     提示：把你自己设为观战者（眼睛图标），AI 全权替你打")
    else:
        print("  4. 1号位(你): 设为观战者（眼睛图标）→ 选 %s" % ais[0])
        print("     2号位: 选 %s" % ais[1] if len(ais) > 1 else "     2号位: 另一个 EvoAI")
    print("  5. 开始游戏，坐等 AI 搏杀（可开游戏内加速）")
    print("=" * 56)
    print()


def ensure_ai_installed(name, cfg, seed=None):
    """若游戏目录里没有该 AI，则用存档基因或默认基因安装。"""
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
    ai_dir = Path(cfg["game"]["ai_dir"])
    per = ai_dir / ("EvoAI_%s.per" % safe)
    if per.exists():
        return "EvoAI_%s" % safe
    # 从 lab_data/generated 找存档基因
    gen_file = ROOT / "lab_data" / "generated" / ("EvoAI_%s.json" % safe)
    if gen_file.exists():
        MK.install(safe, G.load(gen_file), cfg)
    else:
        MK.install(safe, G.default_genome(), cfg)
    return "EvoAI_%s" % safe


def main():
    ap = argparse.ArgumentParser(description="启动一场 AI 对战")
    ap.add_argument("ai1", help="第一个 AI 名称（如 EvoAI_A / Champion）")
    ap.add_argument("ai2", nargs="?", help="第二个 AI 名称（省略则 vs 电脑）")
    ap.add_argument("--vs-cpu", default="hard",
                    choices=["easiest", "easy", "moderate", "hard", "hardest"],
                    help="对手电脑难度（无 ai2 时生效）")
    ap.add_argument("--no-launch", action="store_true", help="只装AI不开游戏")
    ap.add_argument("--no-report", action="store_true", help="结束后不生成战报")
    args = ap.parse_args()

    cfg = load_config()
    name1 = ensure_ai_installed(args.ai1, cfg)
    names = [name1]
    if args.ai2:
        names.append(ensure_ai_installed(args.ai2, cfg))

    before = snapshot_latest_record(cfg)

    if not args.no_launch:
        launch_game(cfg)
    print_lobby_checklist(cfg, names, vs_cpu=args.vs_cpu if not args.ai2 else None)

    if args.no_launch or args.no_report:
        return

    input("[等待] 对局打完后回到这里按回车，自动生成战报...")
    time.sleep(2)
    after = snapshot_latest_record(cfg)
    if after and (before is None or after != before):
        print("[检测] 新录像: %s" % after.name)
        subprocess.call([sys.executable, str(Path(__file__).parent / "report.py"),
                         "--file", str(after)])
    else:
        print("[提示] 没检测到新录像。若对局刚结束请稍等几秒重试：")
        print("       python report.py --latest")


if __name__ == "__main__":
    main()
