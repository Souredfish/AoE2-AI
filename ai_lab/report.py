# -*- coding: utf-8 -*-
"""
report.py — 战报生成器
======================
解析游戏的 .aoe2record 录像文件（依赖 mgz 库），
生成 Markdown 战报 + 结构化 JSON（供 evolve.py 读取积分）。

用法：
  python report.py --latest              # 解析最新一场对局
  python report.py --file <path>          # 解析指定录像
"""

import argparse
import json
import sys
import time
from pathlib import Path

try:
    from .recordings import latest_recording, recording_directories
except ImportError:  # Script execution from ai_lab/ on Windows.
    from recordings import latest_recording, recording_directories

ROOT = Path(__file__).resolve().parent.parent


def load_config():
    with open(ROOT / "config.json", "r", encoding="utf-8") as f:
        return json.load(f)


def find_latest_record(cfg):
    record = latest_recording(cfg)
    if record is None:
        dirs = ", ".join(str(p) for p in recording_directories(cfg))
        sys.exit("没有找到 .aoe2record 录像。已检查目录: %s；请核对 config.json 的 game.recordings_dir" % (dirs or "未配置"))
    return record


def parse_record(path):
    """用 mgz 解析录像，返回结构化对局信息。尽量容错。"""
    try:
        from mgz.summary import Summary
    except ImportError:
        sys.exit("缺少 mgz 库，请先安装: pip install mgz")

    with open(path, "rb") as f:
        s = Summary(f)

    info = {"file": str(path), "mtime": path.stat().st_mtime}
    try:
        info["map"] = s.get_map().get("name", "?")
    except Exception:
        info["map"] = "?"
    try:
        dur = s.get_duration()
        info["duration_min"] = round(dur / 60.0, 1) if dur else None
    except Exception:
        info["duration_min"] = None
    try:
        info["speed"] = s.get_speed()
    except Exception:
        pass

    players = []
    try:
        for p in s.get_players():
            entry = {
                "name": p.get("name", "?"),
                "civ": p.get("civilization", "?"),
                "color": p.get("color_id"),
                "human": p.get("human"),
            }
            # mgz 的 winner 字段（来自 postgame）不一定存在
            if "winner" in p and p["winner"] is not None:
                entry["winner"] = bool(p["winner"])
            players.append(entry)
    except Exception as e:
        raise ValueError("mgz 无法解析录像玩家信息；请确认 mgz 版本支持该游戏 build。原始错误: %s" % e) from e

    info["players"] = players

    # 终局战绩（postgame achievements），尽力提取
    try:
        ach = s.get_achievements()
        if ach:
            for i, a in enumerate(ach):
                if i < len(players):
                    players[i].update({
                        "score": getattr(a, "score", None),
                        "kills": getattr(a, "military_score", None),
                        "razed": getattr(a, "razed_score", None),
                        "total_xp": getattr(a, "total_xp", None),
                    })
    except Exception:
        pass

    # 胜者推断：优先 postgame 的 winner 标记
    explicit = [p["name"] for p in players if p.get("winner")]
    if explicit:
        info["winners"] = explicit
    elif players:
        # 退化方案：都标记为未知，由 evolve.py 人工确认或视为平局
        info["winners"] = []
    else:
        info["winners"] = []
    return info


def fmt_duration(mins):
    if mins is None:
        return "?"
    return "%d分%02d秒" % (mins, (mins % 1) * 60)


def render_markdown(info):
    lines = []
    lines.append("# 战报：%s vs %s" % (
        " vs ".join(p["name"] for p in info["players"][:2]) or "未知对局"))
    lines.append("")
    lines.append("- 地图: %s" % info.get("map", "?"))
    lines.append("- 时长: %s" % fmt_duration(info.get("duration_min")))
    lines.append("- 录像: `%s`" % Path(info["file"]).name)
    lines.append("- 时间: %s" % time.strftime("%Y-%m-%d %H:%M", time.localtime(info["mtime"])))
    lines.append("")
    lines.append("| 玩家 | 文明 | 颜色 | 分数 | 击杀分 | 摧毁分 | 结果 |")
    lines.append("|---|---|---|---|---|---|---|")
    for p in info["players"]:
        result = "胜" if p.get("winner") else ("?" if not info["winners"] else "负")
        lines.append("| %s | %s | P%s | %s | %s | %s | %s |" % (
            p["name"], p.get("civ", "?"), p.get("color"), p.get("score", "-"),
            p.get("kills", "-"), p.get("razed", "-"), result))
    lines.append("")
    if not info["winners"]:
        lines.append("> 未能自动判定胜者（mgz 版本差异），请人工确认后修改 JSON。")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="生成战报")
    ap.add_argument("--file", help="指定录像文件")
    ap.add_argument("--latest", action="store_true", help="解析最新录像")
    args = ap.parse_args()

    cfg = load_config()
    path = Path(args.file) if args.file else find_latest_record(cfg)
    print("[解析] %s" % path)

    info = parse_record(path)
    print(json.dumps(info, ensure_ascii=False, indent=1))

    report_dir = ROOT / "lab_data" / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    stem = time.strftime("%Y%m%d_%H%M%S", time.localtime(info["mtime"]))
    md_path = report_dir / ("%s_battle_report.md" % stem)
    json_path = report_dir / ("%s_battle_report.json" % stem)
    md_path.write_text(render_markdown(info), encoding="utf-8")
    json_path.write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
    print("[战报] %s" % md_path)
    print("[数据] %s" % json_path)


if __name__ == "__main__":
    main()
