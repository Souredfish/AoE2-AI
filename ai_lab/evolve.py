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
import math
import random
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).parent))
import genome as G  # noqa: E402
import make_ai as MK  # noqa: E402
import evo_results as ER  # noqa: E402

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
    ER.save_schedule(LAB, args.gen, pairs)
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
    ec = evo_cfg(cfg)
    gen_file = LAB / "generations" / ("gen_%d.json" % cur)
    pop = len(json.loads(gen_file.read_text(encoding="utf-8")))
    pairs = make_schedule(pop, int(ec.get("matches_per_ai", 3)), random.Random(cur * 7919))
    expected_count = pop * int(ec.get("matches_per_ai", 3)) // 2
    manifest = ER.load_schedule(LAB, cur, pairs, results, pop, expected_count)
    accepted = ER.reconcile_results(results, manifest)
    record = info.get("file")
    canonical_record = str(Path(record).resolve()) if record else None
    record_id = ER.match_id_for_recording(canonical_record) if canonical_record and Path(canonical_record).is_file() else None
    for row in accepted.values():
        same_path = canonical_record and row.get("record") and str(Path(row["record"]).resolve()).casefold() == canonical_record.casefold()
        if (record_id and row.get("record_id") == record_id) or same_path:
            print("[幂等] 该录像已记入单局 %s，不重复计分" % row["match_id"])
            return
    try:
        result = result_row_from_report(
            info, cur, pop, manifest, accepted, canonical_record, record_id)
    except ValueError as e:
        sys.exit("[拒绝] 战报未写入账本：%s" % e)
    results.append(result)
    try:
        ER.reconcile_results(results, manifest)
    except ValueError as e:
        results.pop()
        sys.exit("[拒绝] 结果未写入账本：%s" % e)
    ER.write_results(res_file, results)
    print("[记账] 第 %d 代已记录 %d 场对局 → %s" % (cur, len(results), res_file))


def result_row_from_report(info, gen, pop, manifest, accepted,
                           canonical_record=None, record_id=None):
    """Convert report identities into the scheduled ledger order, independent of replay order."""
    indices = [ER.individual_from_name(p.get("name"), gen, pop)
               for p in info.get("players", [])]
    indices = [index for index in indices if index is not None]
    if len(indices) != 2 or indices[0] == indices[1]:
        raise ValueError("战报未能解析为本代两个不同参赛个体")

    scheduled = next((match for match in manifest["matches"]
                      if frozenset(ER.individual_from_name(name, gen, pop)
                                   for name in match["players"]) == frozenset(indices)
                      and match["match_id"] not in accepted), None)
    if scheduled is None:
        raise ValueError("该参赛组合不在未完成赛程中")

    winners = info.get("winners") or []
    winner_ids = [ER.individual_from_name(name, gen, pop) for name in winners]
    if len(winner_ids) != 1 or winner_ids[0] not in indices:
        raise ValueError("战报胜者无效或无法唯一确定；必须且只能有一个参赛胜者")

    player_winner_ids = []
    has_player_winner_flags = False
    for player in info.get("players", []):
        if "winner" not in player:
            continue
        has_player_winner_flags = True
        flag = player["winner"]
        if not isinstance(flag, bool):
            raise ValueError("战报玩家胜者标记必须是布尔值")
        if flag:
            player_id = ER.individual_from_name(player.get("name"), gen, pop)
            if player_id not in indices:
                raise ValueError("战报非参赛玩家不能标记为胜者")
            player_winner_ids.append(player_id)
    if has_player_winner_flags and player_winner_ids != winner_ids:
        raise ValueError("玩家胜者标记与战报 winners 不一致")

    winner = ER.individual_name(gen, winner_ids[0])
    players = scheduled["players"]
    scores = {}
    for player in info.get("players", []):
        index = ER.individual_from_name(player.get("name"), gen, pop)
        if index is not None:
            scores[ER.individual_name(gen, index)] = player.get("score")
    return {
        "match_id": scheduled["match_id"],
        "players": [[players[0], winner == players[0]], [players[1], winner == players[1]]],
        "winners": [winner],
        "scores": scores,
        "duration_min": info.get("duration_min"),
        "record": canonical_record,
        "record_id": record_id,
    }


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
    pairs = make_schedule(len(gene_pop), int(ec.get("matches_per_ai", 3)), random.Random(cur * 7919))
    expected_count = len(gene_pop) * int(ec.get("matches_per_ai", 3)) // 2
    manifest = ER.load_schedule(LAB, cur, pairs, results, len(gene_pop), expected_count)
    try:
        accepted = ER.validate_complete(results, manifest, len(gene_pop), expected_count)
    except ValueError as e:
        sys.exit("第 %d 代结果未通过完整性校验，拒绝繁殖：%s" % (cur, e))

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
            if name == winners[0]:
                wins[gi] += 1
            sc = scores.get(name)
            opp_sc = None
            for j, (n2, _) in enumerate(r["players"]):
                if n2 != name:
                    opp_sc = scores.get(n2)
            if (isinstance(sc, (int, float)) and math.isfinite(sc)
                    and isinstance(opp_sc, (int, float)) and math.isfinite(opp_sc)):
                margins[gi] += (sc - opp_sc) / 10000.0
    fitness = [(wins[i] + margins[i], i) if math.isfinite(wins[i] + margins[i])
               else (float("-inf"), i) for i in range(pop)]
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
    score_file = gen_dir / "champion_fitness.json"
    previous_score = _load_champion_fitness(score_file)
    has_champion = champ_file.exists()
    if _should_update_champion(fitness[0][0], previous_score, has_champion):
        G.save(champ, champ_file)
        score_file.write_text(json.dumps({"fitness": fitness[0][0], "generation": cur}, indent=1), encoding="utf-8")
        MK.install("Champion", champ, cfg)
        print("历史最佳基因已更新并安装为 EvoAI_Champion（G%dP%d）" % (cur, best_i))
    elif has_champion and previous_score is None:
        print("保留现有冠军：旧冠军档案没有可比较的适应度；本代成绩不会自动替换它。")
    elif not math.isfinite(fitness[0][0]):
        print("本代最佳适应度不是有限数值，未写入冠军档案。")

    print()
    print("第 %d 代已生成并安装。对战表：" % nxt)
    pairs = make_schedule(pop, int(ec.get("matches_per_ai", 3)), rng)
    ER.save_schedule(LAB, nxt, pairs)
    for a, b in pairs:
        print("   EvoAI_G%dP%d  vs  EvoAI_G%dP%d" % (nxt, a, nxt, b))
    print("继续：python run_match.py EvoAI_G%dP0 EvoAI_G%dP1 ..." % (nxt, nxt))


def _match_individual(name, gen, pop):
    return ER.individual_from_name(name, gen, pop)


def _tournament(fitness, k, rng):
    """With-replacement tournament; fitness rows are (score, individual_index)."""
    best_score, best = rng.choice(fitness)
    for _ in range(k - 1):
        score, candidate = rng.choice(fitness)
        if score > best_score:
            best_score, best = score, candidate
    return best


def _should_update_champion(candidate_fitness, previous_fitness, has_champion):
    """Legacy champions lack scores, so preserve them until manually evaluated."""
    if not math.isfinite(candidate_fitness):
        return False
    if not has_champion:
        return True
    return previous_fitness is not None and candidate_fitness > previous_fitness


def _load_champion_fitness(path):
    """Return only finite champion scores; malformed or legacy metadata is unknown."""
    try:
        fitness = float(json.loads(path.read_text(encoding="utf-8"))["fitness"])
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return fitness if math.isfinite(fitness) else None


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
