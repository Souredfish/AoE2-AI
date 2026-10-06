# -*- coding: utf-8 -*-
"""
make_ai.py — 把基因组渲染成可上场的 AI
==========================================
原理：以游戏自带的官方 AiBuilder.per 为底座，
用基因组里的 phaseX-* 常量值替换其中的 (defconst ...) 默认值，
再注入「按时代自动推进相位」的规则（官方模板默认靠场景信号推进，
独立对局不会推进相位，这是我们补上的关键逻辑），
最后在游戏 ai 目录生成 EvoAI_<Name>.per + EvoAI_<Name>.ai 一对文件。

用法：
  python make_ai.py --name Champion --mode default     # 官方默认风格
  python make_ai.py --name G0P3 --mode random --seed 3 # 随机个体
  python make_ai.py --name Champion --genome lab_data/generations/champion.json
"""

import argparse
from collections import Counter
import hashlib
import json
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import genome as G  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CONSTANT_RE = re.compile(r"\(defconst\s+(phase[1-5]-[A-Za-z0-9_-]+)\s+[-+\d.]+\s*\)")


def expected_constants():
    return {tmpl.format(n=ph) for tmpl, _, _, _ in G.SPEC for ph in G.PHASES}


def constants_fingerprint(names):
    payload = "\n".join(sorted(names)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def load_config():
    with open(ROOT / "config.json", "r", encoding="utf-8") as f:
        return json.load(f)


# 注入到生成脚本末尾的相位推进规则：
# 官方 AiBuilder 的相位只受场景 AI 信号控制，单机对局永远停在 Phase 1，
# 这里改为按时代推进，帝王时代 30 分钟后进入最终相位 Phase 5。
PHASE_RULES = """
; ==== EvoLab: advance phases by age ====
(defrule
	(goal current-phase 1)
	(current-age >= feudal-age)
=>
	(set-goal current-phase 2)
	(chat-local-to-self "EvoLab: Phase 2 (Feudal)"))
(defrule
	(goal current-phase 2)
	(current-age >= castle-age)
=>
	(set-goal current-phase 3)
	(chat-local-to-self "EvoLab: Phase 3 (Castle)"))
(defrule
	(goal current-phase 3)
	(current-age >= imperial-age)
=>
	(set-goal current-phase 4)
	(chat-local-to-self "EvoLab: Phase 4 (Imperial)"))
(defrule
	(goal current-phase 4)
	(up-compare-goal gl-game-time g:>= 1800)
=>
	(set-goal current-phase 5)
	(chat-local-to-self "EvoLab: Phase 5 (Late Imperial)"))
"""

INTRO_RULE = """
; ==== EvoLab: identity ====
(defrule
	(true)
=>
	(chat-to-all "EVOLAB __NAME__ #__HASH__")
	(disable-self))
"""


def gene_hash(gene):
    """基因组的短指纹：开局聊天播报用，用于核对录像里实际跑的是哪个基因。"""
    import hashlib
    raw = "|".join("%s=%d" % (k, gene[k]) for k in sorted(gene))
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:8]


def build_per_text(base_text, gene, name):
    """校验 AiBuilder 参数面版本并完整替换后返回生成脚本。"""
    gene = G._normalize(G.clamp(gene))
    expected = expected_constants()
    definitions = Counter(CONSTANT_RE.findall(base_text))
    actual = {name for name in definitions if name in expected}
    expected_fingerprint = constants_fingerprint(expected)
    actual_fingerprint = constants_fingerprint(actual)
    missing = sorted(expected - actual)
    duplicates = sorted(name for name in expected if definitions[name] > 1)
    if missing or duplicates:
        raise ValueError(
            "AiBuilder 目标参数缺失或重复 (expected=%s actual=%s missing=%d duplicate=%d)"
            % (expected_fingerprint[:12], actual_fingerprint[:12], len(missing), len(duplicates))
        )
    if set(gene) != expected:
        raise ValueError("基因常量集合不完整：expected=%d actual=%d" % (len(expected), len(gene)))

    text = base_text
    for const_name, value in sorted(gene.items()):
        pattern = re.compile(
            r"\(defconst\s+" + re.escape(const_name) + r"\s+[-\d.]+\s*\)"
        )
        repl = "(defconst %s %d)" % (const_name, int(value))
        matches = pattern.findall(text)
        if len(matches) != 1:
            raise ValueError("AiBuilder 常量必须且只能定义一次：%s (count=%d)" % (const_name, len(matches)))
        text = pattern.sub(repl, text, count=1)
    header = (
        "; ==== EvoLab generated AI: %s ====\n"
        "; Based on official AiBuilder, parameters evolved by genetic algorithm\n\n" % name
    )
    text = header + text + INTRO_RULE.replace("__NAME__", name).replace("__HASH__", gene_hash(gene)) + PHASE_RULES
    return text


def install(name, gene, cfg):
    gene = G._normalize(G.clamp(gene))
    ai_dir = Path(cfg["game"]["ai_dir"])
    base_path = ai_dir / cfg["game"]["base_script"]
    if not base_path.exists():
        sys.exit("找不到底座脚本: %s" % base_path)
    base_text = base_path.read_text(encoding="latin-1")

    # Validate everything before writing either generated file.
    per_text = build_per_text(base_text, gene, name)

    safe = re.sub(r"[^A-Za-z0-9_-]", "_", name)
    per_path = ai_dir / ("EvoAI_%s.per" % safe)
    ai_path = ai_dir / ("EvoAI_%s.ai" % safe)
    per_path.write_text(per_text, encoding="latin-1")
    ai_path.write_text("", encoding="latin-1")
    h = gene_hash(gene)
    print("[完成] hash=%s  AI 安装到：" % h)
    print("       %s" % per_path)
    print("       %s" % ai_path)
    return safe


def main():
    ap = argparse.ArgumentParser(description="生成并安装一个 EvoAI")
    ap.add_argument("--name", required=True, help="AI 名称（游戏下拉框显示 EvoAI_<name>）")
    ap.add_argument("--mode", choices=["default", "random"], default="default")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--genome", help="基因组 JSON 文件路径（优先于 --mode）")
    args = ap.parse_args()

    cfg = load_config()
    if args.genome:
        gene = G.load(args.genome)
        print("[基因] 从 %s 载入" % args.genome)
    elif args.mode == "random":
        rng = random.Random(args.seed)
        gene = G.random_genome(rng)
        print("[基因] 随机生成 (seed=%s)" % args.seed)
    else:
        gene = G.default_genome()
        print("[基因] 使用官方默认风格")

    print(G.describe(gene))
    safe = install(args.name, gene, cfg)

    # 存档基因，便于复现与进化
    gen_dir = ROOT / "lab_data" / "generated"
    gen_dir.mkdir(parents=True, exist_ok=True)
    out = gen_dir / ("EvoAI_%s.json" % safe)
    G.save(gene, out)
    print("[存档] 基因组已保存: %s" % out)


if __name__ == "__main__":
    main()
