# -*- coding: utf-8 -*-
"""
evolve.py — 遗传算法主循环（半自动进化流水线）
===============================================
完整一代的流程：

  ① init   生成第 0 代种群（随机基因），安装进游戏，打印对战表
  ② 你在游戏里跑对战表（用 run_match.py 或自己建房，建议开加速）
     每打完一局： python evolve.py report   （自动解析最新录像记账）
  ③ next   积分汇总 → 精英保留 + 锦标赛选择 → 交叉 + 变异
           生成下一代种群并安装，回到 ②
  ④ champion  把当前最佳基因安装为 EvoAI_Champion（替你打电脑用）

数据落盘在 lab_data/：
  generations/gen_N.json    每代全部个体的基因
  results/gen_N.json        每代对局结果
  generations/champion.json 历史最佳基因
"""

import argparse
import json
import random
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).parent))
import genome as G  # noqa: E402
import make_ai as MK  # noqa: E402

LAB = ROOT / "lab_data"


def load_config():
    with open(ROOT / "config.json", "r", encoding="utf-8") as f:
        return json.load(f)


def evo_cfg(cfg):
    return cfg.get("evolution", {})


# ------------------------------------------------------------------
# ① init
# ------------------------------------------------------------------
def cmd_init(args, cfg):
    ec = evo_cfg(cfg)
    pop = int(ec.get("population", 8))
    rng = random.Random(args.seed or None)
    gene_pop = [G.random_genome(rng) for _ in range(pop)]
    # 个体 0 用官方默认风格做锚点，衡量进化是否真的变强
    gene_pop[0] = G.default_genome()

    gen_dir = LAB / "generations"
    gen_dir.mkdir(parents=True, exist_ok=True)
    generated_dir = LAB / "generated"
    generated_dir.mkdir(parents=True, exist_ok=True)
    for i, gene in enumerate(gene_pop):
        safe_name = "G%dP%d" % (args.gen, i)
        MK.install(safe_name, gene, cfg)
        # 单基因存档，让 make_ai.py --genome / auto_runner.py 都能按名加载
        G.save(gene, generated_dir / ("EvoAI_%s.json" % safe_name))
    (gen_dir / ("gen_%d.json" % args.gen)).write_text(
        json.dumps(gene_pop, indent=1), encoding="utf-8")

    print()
    print("第 %d 代种群就绪（%d 个个体，G%dP0 为官方默认锚点）：" % (args.gen, pop, args.gen))
    pairs = make_schedule(pop, int(ec.get("matches_per_ai", 3)), rng)
    print("对战表（共 %d 场）：" % len(pairs))
    for a, b in pairs:
        print("   EvoAI_G%dP%d  vs  EvoAI_G%dP%d" % (args.gen, a, args.gen, b))
    print()
    print("逐场执行：python run_match.py EvoAI_G%dP0 EvoAI_G%dP1" % (args.gen, args.gen))
    print("打完每场后：python evolve.py report")


def make_schedule(pop, matches_per_ai, rng):
    """随机配对赛程，保证每个个体约打 matches_per_ai 场。"""
    total_matches = pop * matches_per_ai // 2
    pairs, tries = [], 0
    while len(pairs) < total_matches and tries < 1000:
        tries += 1
        a, b = rng.sample(range(pop), 2)
        if (a, b) not in pairs and (b, a) not in pairs:
            pairs.append((a, b))
    return pairs


# ------------------------------------------------------------------
# ② report
# ------------------------------------------------------------------
def cmd_report(args, cfg):
    out = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "report.py"), "--latest"],
        capture_output=True, text=True)
    print(out.stdout)
    if out.returncode != 0:
        print(out.stderr)
        return

    # 找到刚生成的 json，登记进当前代的结果
    report_dir = LAB / "reports"
    jsons = sorted(report_dir.glob("*_battle_report.json"),
                   key=lambda p: p.stat().st_mtime)
    if not jsons:
        print("[错误] 没找到战报 JSON")
        return
    info = json.loads(jsons[-1].read_text(encoding="utf-8"))

    cur = current_gen()
    res_dir = LAB / "results"
    res_dir.mkdir(parents=True, exist_ok=True)
    res_file = res_dir / ("gen_%d.json" % cur)
    results = json.loads(res_file.read_text(encoding="utf-8")) if res_file.exists() else []
    results.append({
        "players": [(p["name"], bool(p.get("winner"))) for p in info.get("players", [])],
        "winners": info.get("winners", []),
        "scores": {p["name"]: p.get("score") for p in info.get("players", [])},
        "duration_min": info.get("duration_min"),
        "record": info.get("file"),
    })
    res_file.write_text(json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8")
    print("[记账] 第 %d 代已记录 %d 场对局 → %s" % (cur, len(results), res_file))


def current_gen():
    gens = sorted((LAB / "generations").glob("gen_*.json"))
    if not gens:
        return 0
    return int(gens[-1].stem.split("_")[1])


# ------------------------------------------------------------------
# ③ next
# ------------------------------------------------------------------
def cmd_next(args, cfg):
    ec = evo_cfg(cfg)
    cur = current_gen()
    gen_file = LAB / "generations" / ("gen_%d.json" % cur)
    res_file = LAB / "results" / ("gen_%d.json" % cur)
    if not gen_file.exists():
        sys.exit("没有第 %d 代种群，先运行 evolve.py init" % cur)
    if not res_file.exists():
        sys.exit("第 %d 代还没有任何对局结果，先跑对战并用 evolve.py report 记账" % cur)

    gene_pop = json.loads(gen_file.read_text(encoding="utf-8"))
    results = json.loads(res_file.read_text(encoding="utf-8"))

    # ---- 积分 ----
    pop = len(gene_pop)
    wins = [0] * pop
    games = [0] * pop
    margins = [0.0] * pop
    for r in results:
        names = [n for n, _ in r["players"]]
        winners = r.get("winners") or []
        if not winners:
            continue
        scores = r.get("scores", {})
        for i, (name, _) in enumerate(r["players"]):
            gi = _match_individual(name, cur, pop)
            if gi is None:
                continue
            games[gi] += 1
            if any(w in name for w in winners):
                wins[gi] += 1
            sc = scores.get(name)
            opp_sc = None
            for j, (n2, _) in enumerate(r["players"]):
                if n2 != name:
                    opp_sc = scores.get(n2)
            if isinstance(sc, (int, float)) and isinstance(opp_sc, (int, float)):
                margins[gi] += (sc - opp_sc) / 10000.0
    fitness = [(wins[i] + margins[i], i) for i in range(pop)]
    fitness.sort(reverse=True)
    print("第 %d 代积分榜（胜场/分差加成）：" % cur)
    for f, i in fitness:
        print("   G%dP%-2d  %2d胜 /%2d场   基因:%s" % (
            cur, i, wins[i], games[i], LAB / "generations"))

    # ---- 繁殖下一代 ----
    rng = random.Random()
    evo_cfg_mut = evo_cfg(cfg)
    rate = float(evo_cfg_mut.get("mutation_rate", 0.35))
    sigma = float(evo_cfg_mut.get("mutation_sigma", 0.15))
    elite = int(evo_cfg_mut.get("elite_count", 2))
    new_pop = [gene_pop[i] for _, i in fitness[:elite]]  # 精英直通
    while len(new_pop) < pop:
        # 锦标赛选择（k=3）
        pa = _tournament(fitness, 3, rng)
        pb = _tournament(fitness, 3, rng)
        child = G.crossover(gene_pop[pa], gene_pop[pb], rng)
        child = G.mutate(child, rng, rate, sigma)
        new_pop.append(child)

    nxt = cur + 1
    gen_dir = LAB / "generations"
    generated_dir = LAB / "generated"
    generated_dir.mkdir(parents=True, exist_ok=True)
    for i, gene in enumerate(new_pop):
        safe_name = "G%dP%d" % (nxt, i)
        MK.install(safe_name, gene, cfg)
        G.save(gene, generated_dir / ("EvoAI_%s.json" % safe_name))
    (gen_dir / ("gen_%d.json" % nxt)).write_text(
        json.dumps(new_pop, indent=1), encoding="utf-8")

    # ---- 更新历史最佳 ----
    best_i = fitness[0][1]
    champ = gene_pop[best_i]
    champ_file = gen_dir / "champion.json"
    prev = json.loads(champ_file.read_text(encoding="utf-8")) if champ_file.exists() else None
    if prev is None or fitness[0][0] > 0:
        G.save(champ, champ_file)
        MK.install("Champion", champ, cfg)
        print("历史最佳基因已更新并安装为 EvoAI_Champion（G%dP%d）" % (cur, best_i))

    print()
    print("第 %d 代已生成并安装。对战表：" % nxt)
    pairs = make_schedule(pop, int(ec.get("matches_per_ai", 3)), rng)
    for a, b in pairs:
        print("   EvoAI_G%dP%d  vs  EvoAI_G%dP%d" % (nxt, a, nxt, b))
    print("继续：python run_match.py EvoAI_G%dP0 EvoAI_G%dP1 ..." % (nxt, nxt))


def _match_individual(name, gen, pop):
    for i in range(pop):
        if "G%dP%d" % (gen, i) in name:
            return i
    return None


def _tournament(fitness, k, rng):
    best = rng.choice(fitness)[1]
    for _ in range(k - 1):
        cand = rng.choice(fitness)[1]
        if cand < best:
            best = cand
    return best


# ------------------------------------------------------------------
# ④ champion
# ------------------------------------------------------------------
def cmd_champion(args, cfg):
    champ_file = LAB / "generations" / "champion.json"
    if not champ_file.exists():
        sys.exit("还没有冠军档案。先跑完至少一代：evolve.py init → 对战 → evolve.py next")
    gene = G.load(champ_file)
    print(G.describe(gene))
    MK.install("Champion", gene, cfg)
    print()
    print("用冠军替你打电脑：")
    print("  python run_match.py Champion --vs-cpu hardest")


def main():
    ap = argparse.ArgumentParser(description="EvoAI 遗传进化主控")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_init = sub.add_parser("init", help="生成并安装第 0 代种群")
    p_init.add_argument("--gen", type=int, default=0)
    p_init.add_argument("--seed", type=int, default=None)
    sub.add_parser("report", help="解析最新对局并计入当前代积分")
    sub.add_parser("next", help="汇总积分，进化出下一代")
    sub.add_parser("champion", help="安装历史最佳 AI（替你打电脑）")
    args = ap.parse_args()

    cfg = load_config()
    {"init": cmd_init, "report": cmd_report,
     "next": cmd_next, "champion": cmd_champion}[args.cmd](args, cfg)


if __name__ == "__main__":
    main()
