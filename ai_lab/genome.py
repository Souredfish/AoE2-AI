# -*- coding: utf-8 -*-
"""
genome.py — EvoAI 基因组定义
=================================
每个 AI 的"基因"就是一组 phaseX-* 常量取值（基于官方 AiBuilder 框架）。
对战双方使用同一套底层规则引擎，基因差异决定行为差异，
遗传算法通过 胜负选择 -> 交叉 -> 高斯变异 迭代出更强的基因。

相位对应时代（由 make_ai.py 注入的推进规则驱动）：
  Phase 1 = 黑暗时代   Phase 2 = 封建时代
  Phase 3 = 城堡时代   Phase 4 = 帝王时代前期  Phase 5 = 帝王时代后期
"""

import json
import random
from pathlib import Path

# ------------------------------------------------------------------
# 基因规格：name 模板 / 取值范围 / 各相位默认值（5 个相位）
# 说明：
#  - 单位 cap 全部进化 *-hard 系列，进化对局固定 Hard 难度
#  - 采集百分比(food/wood/gold/stone)每相位自动归一化到 100
# ------------------------------------------------------------------
SPEC = [
    # ---- 经济 ----
    ("phase{n}-villager-cap-hard",            8, 110, [20, 30, 55, 70, 85]),
    ("phase{n}-food-gatherers",              25,  75, [55, 50, 45, 45, 45]),
    ("phase{n}-wood-gatherers",              10,  50, [30, 32, 34, 34, 34]),
    ("phase{n}-gold-gatherers",               5,  45, [15, 18, 21, 21, 21]),
    ("phase{n}-stone-gatherers",              0,  20, [0, 0, 0, 0, 0]),
    ("phase{n}-age-villager-requirement",     6,  40, [9, 14, 20, 28, 40]),
    ("phase{n}-towncenter-cap",               1,   5, [1, 1, 2, 3, 4]),
    # ---- 军事产能 ----
    ("phase{n}-barracks-cap",                 1,   4, [1, 1, 1, 2, 2]),
    ("phase{n}-range-cap",                    1,   4, [1, 1, 2, 2, 3]),
    ("phase{n}-stable-cap",                   1,   4, [1, 1, 2, 2, 3]),
    # ---- 兵种构成 ----
    ("phase{n}-militia-cap-hard",             0,  20, [0, 0, 0, 0, 0]),
    ("phase{n}-spearman-cap-hard",            0,  25, [0, 0, 6, 8, 10]),
    ("phase{n}-skirmisher-cap-hard",          0,  20, [0, 0, 4, 6, 8]),
    ("phase{n}-archer-cap-hard",              0,  30, [0, 4, 10, 16, 20]),
    ("phase{n}-scoutcavalry-cap-hard",        0,   8, [1, 2, 1, 1, 1]),
    ("phase{n}-knight-cap-hard",              0,  30, [0, 0, 10, 14, 18]),
    ("phase{n}-camelrider-cap-hard",          0,  20, [0, 0, 0, 0, 0]),
    ("phase{n}-steppelancer-cap-hard",        0,  20, [0, 0, 0, 0, 0]),
    ("phase{n}-battleelephant-cap-hard",      0,  20, [0, 0, 0, 0, 0]),
    ("phase{n}-cavalryarcher-cap-hard",       0,  25, [0, 0, 0, 4, 6]),
    ("phase{n}-handcannoneer-cap-hard",       0,  15, [0, 0, 0, 0, 5]),
    ("phase{n}-monk-cap-hard",                0,  12, [0, 0, 0, 0, 2]),
    ("phase{n}-uniqueunit-cap-hard",          0,  20, [0, 0, 4, 6, 8]),
    # ---- 攻城武器 ----
    ("phase{n}-mangonel-cap-hard",            0,   8, [0, 0, 1, 2, 2]),
    ("phase{n}-batteringram-cap-hard",        0,   8, [0, 0, 0, 1, 2]),
    ("phase{n}-trebuchet-cap-hard",           0,   8, [0, 0, 0, 1, 2]),
    ("phase{n}-scorpion-cap-hard",            0,   8, [0, 0, 0, 0, 1]),
    ("phase{n}-bombardcannon-cap-hard",       0,   6, [0, 0, 0, 0, 1]),
    # ---- 进攻行为 ----
    ("phase{n}-land-attack-percentage",       0, 100, [60, 70, 80, 85, 90]),
    ("phase{n}-scale-attack-timer-hard",     20, 300, [80, 70, 60, 50, 40]),
]

PHASES = [1, 2, 3, 4, 5]
GATHERERS = ("food-gatherers", "wood-gatherers", "gold-gatherers", "stone-gatherers")


def default_genome():
    """官方默认风格的基础基因组。"""
    g = {}
    for tmpl, lo, hi, defaults in SPEC:
        for i, ph in enumerate(PHASES):
            g[tmpl.format(n=ph)] = int(defaults[i])
    return _normalize(g)


def random_genome(rng):
    """均匀随机基因组（第 0 代多样性来源）。"""
    g = {}
    for tmpl, lo, hi, defaults in SPEC:
        for ph in PHASES:
            g[tmpl.format(n=ph)] = int(rng.randint(lo, hi))
    return _normalize(g)


def mutate(genome, rng, rate=0.35, sigma=0.15):
    """高斯变异：每个基因以 rate 概率被扰动，幅度为值域宽度的 sigma 倍。"""
    g = dict(genome)
    for tmpl, lo, hi, _ in SPEC:
        for ph in PHASES:
            name = tmpl.format(n=ph)
            if rng.random() < rate:
                width = hi - lo
                g[name] = int(round(g[name] + rng.gauss(0, sigma * width)))
    return _normalize(clamp(g))


def crossover(a, b, rng):
    """均匀交叉：每个基因随机取自父代 A 或 B。"""
    g = {}
    for name in a:
        g[name] = a[name] if rng.random() < 0.5 else b.get(name, a[name])
    return _normalize(clamp(g))


def clamp(genome):
    g = dict(genome)
    for tmpl, lo, hi, _ in SPEC:
        for ph in PHASES:
            name = tmpl.format(n=ph)
            if name in g:
                g[name] = int(max(lo, min(hi, g[name])))
    return g


def _normalize(genome):
    """每个相位的四类采集百分比归一化到 100，保证经济引擎拿到合法配比。"""
    g = dict(genome)
    for ph in PHASES:
        keys = [f"phase{ph}-{res}" for res in GATHERERS]
        vals = [max(0, int(g.get(k, 0))) for k in keys]
        total = sum(vals)
        if total <= 0:
            vals = [55, 30, 15, 0]
            total = 100
        scaled = [v * 100 // total for v in vals]
        scaled[-2] += 100 - sum(scaled)  # 余数补到金矿，误差 ±1 可接受
        for k, v in zip(keys, scaled):
            g[k] = int(v)
    return g


def save(genome, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(genome, f, indent=1, sort_keys=True, ensure_ascii=False)


def load(path):
    # 既支持绝对路径，也支持相对 ai_lab/ 工作目录的 lab_data/...
    p = Path(path)
    if not p.exists():
        # ai_lab/lab_data/... → 项目根/lab_data/...
        p2 = Path(__file__).resolve().parent.parent / "lab_data" / Path(path).name
        if p2.exists():
            p = p2
        else:
            # 直接相对项目根
            p3 = Path(__file__).resolve().parent.parent / path
            if p3.exists():
                p = p3
    with open(p, "r", encoding="utf-8") as f:
        return _normalize(clamp({k: int(v) for k, v in json.load(f).items()}))


def describe(genome):
    """人类可读的基因摘要（用于战报和存档）。"""
    lines = []
    for ph in PHASES:
        lines.append(
            "P{} 封顶村民{:<3d} 采集比 食{:<3d}/木{:<3d}/金{:<3d}/石{:<3d} "
            "进攻{:<3d}% 攻击间隔{:<3d}".format(
                ph,
                genome.get(f"phase{ph}-villager-cap-hard", 0),
                genome.get(f"phase{ph}-food-gatherers", 0),
                genome.get(f"phase{ph}-wood-gatherers", 0),
                genome.get(f"phase{ph}-gold-gatherers", 0),
                genome.get(f"phase{ph}-stone-gatherers", 0),
                genome.get(f"phase{ph}-land-attack-percentage", 0),
                genome.get(f"phase{ph}-scale-attack-timer-hard", 0),
            )
        )
    comps = []
    for unit in ("militia", "spearman", "skirmisher", "archer", "knight",
                 "cavalryarcher", "handcannoneer", "uniqueunit", "monk"):
        v3 = genome.get(f"phase3-{unit}-cap-hard", 0)
        v5 = genome.get(f"phase5-{unit}-cap-hard", 0)
        if v3 or v5:
            comps.append(f"{unit} P3:{v3}/P5:{v5}")
    lines.append("兵种: " + ", ".join(comps))
    return "\n".join(lines)


if __name__ == "__main__":
    g = default_genome()
    print(describe(g))
