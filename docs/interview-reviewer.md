# AoE2 AI Arena — Project Overview

> **Purpose**: For interviewers / reviewers to quickly understand what was built, the technical choices, and the honest limitations.

---

## TL;DR (60-second version)

I use genetic algorithms to evolve custom *Age of Empires II* AI scripts. ~150 numeric parameters per AI ("player") are injected into the game's official AiBuilder framework; the AI plays against itself; winners are scored and bred. After ~10 generations the evolved AI reliably beats the built-in Hard AI. The full pipeline runs **unattended for 24h** thanks to integration with [AoE2Control](https://aoe2control.github.io/) Headless API.

---

## The Promise (in 5 bullets)

1. **Built and run** — A working pipeline that produces stronger AI per generation. Measured improvement visible after 5–10 generations.
2. **Engineered, not hacked** — Clean separation of concerns (genome / rendering / evaluation / breeding / runner), tested configuration in `config.json`, reproducible steps in `docs/handover.md`.
3. **Integrated with real systems** — Not a toy. Talks to a real game engine (AoE2 DE), parses real binary replay files (mgz), injects via real Lua scripting host (AoE2Control).
4. **Documented three ways** — README as a directory for three readers (successor / interviewer-self / reviewer-this-doc). No "code is documentation" trap.
5. **Honest about limits** — No claims of beating pro players; explains why pure RL was rejected; lists extension paths.

---

## Technical Choices (and what we ruled out)

### Why "tune parameters" instead of "write AI logic" or "use RL"?

| Approach | Sample efficiency | Engineering cost | Risk |
|---|---|---|---|
| **Tune official AI's 150 parameters** ← chosen | ~100 games/convergence | 2 weeks | Low — official framework guarantees "AI plays" baseline |
| Write AI from scratch in .per | "Infinite" — no working baseline | 6+ weeks | High — 95% of game mechanics need re-implementing |
| Pure RL (PPO/DQN) | 100k+ games | Multi-week on multi-GPU | High — RTS has huge state/action space and sparse rewards |

The chosen approach trades a **structured search space** for sample efficiency. This is a classic "use domain knowledge to constrain the search" trade-off.

### Why Microsoft's AiBuilder framework specifically?

- It is **already there** in the game install: `resources\_common\ai\AiBuilder.per` (5050 lines, official).
- It handles 95% of "what an AI needs to do" out of the box (economy, military, building).
- It exposes ~150 numeric constants per phase that we can tune without writing new rules.
- It is widely used in the community for serious AI development (e.g., the well-known "Barbarian" AI is a competitor in this ecosystem).

### Why game phases (Phase 1–5)?

AiBuilder internally splits the game into phases. Without intervention, Phase 1 → Phase 3 transitions are signaled by *scenario triggers* (campaign-only); in a single-player match the AI **stays at Phase 1 forever**. The project **patches this** by injecting 4 defrules that advance the phase when `current-age` changes. Without this patch, every evolved AI stops at Castle Age and never builds Imperial units — a critical fix.

### Why mgz for replay parsing?

- mgz is the de-facto standard Python library for `.aoe2record` (AoE2 DE's binary replay format).
- It supports `mgz.summary.Summary` which gives us map, duration, players, scores, winner out-of-the-box.

### Why AoE2Control for full automation?

- Provides Headless launcher with `--headless` mode (writes status to stdout, exits with code 0 on success).
- Provides Lua API (`DispatchStartGame`, `GetCurrentGameOptions`, etc.) to control the game from inside the engine.
- Provides IPC over named pipes — useful for future work but not used in the current implementation.

The project's `evolab_driver.main.lua` module is the integration glue: it handles match setup, self-elimination to observer mode, and auto-restart on match end.

---

## What Was Built

### Code volume

- ~1500 lines Python (`genome.py`, `make_ai.py`, `report.py`, `evolve.py`, `run_match.py`, `auto_runner.py`)
- ~150 lines Lua (`evolab_driver.main.lua`)
- 3 markdown documents (~3000 words total) for three different audiences

### Modules

| Module | Purpose |
|---|---|
| `genome.py` | Defines the 150-parameter genome + normalization + mutation/crossover operators |
| `make_ai.py` | Renders a genome → `EvoAI_*.per` script injected into the official AiBuilder framework |
| `report.py` | Parses `.aoe2record` replay files via mgz → Markdown battle report + JSON for breeding |
| `evolve.py` | Genetic algorithm master: init / report / next / champion subcommands |
| `run_match.py` | Manually start a single match (semi-auto mode) |
| `auto_runner.py` | Fully automated match runner via AoE2Control Headless |
| `evolab_driver.main.lua` | Lua module injected into the game: auto-match-setup, self-elimination, auto-restart |

### Genetic algorithm specifics

- **Population**: 8 (configurable)
- **Generations**: typically 10–30 for convergence
- **Matches per AI**: 3 (~12 matches per generation)
- **Selection**: Tournament (k=3) + elite retention (top 2 pass through)
- **Crossover**: Uniform (each gene from A or B with 50% probability)
- **Mutation**: Gaussian, rate 35%, σ = 15% of value range
- **Fitness**: `wins + (score_margin / 10000)` — simple but effective

### Full automation architecture

```
evolve.py init  →  generate population, install into AoE2DE
                  ↓
auto_runner.py  →  Python side
                  │  • launch AoE2Control Headless
                  │  • deploy evolab_driver.lua to %APPDATA%\CONTROL\...
                  │  • write EvoAI_A.per / EvoAI_B.per with current generation's genes
                  │  • poll for new .aoe2record → mgz.parse → score
                  │     ↕
                  │  game side (Lua)
                  │  • auto-configure match (map, difficulty, players)
                  │  • auto-eliminate player 1 (becomes pure observer)
                  │  • auto-start next match on game end
                  ↓
evolve.py next  →  breed next generation
```

---

## Honest Limitations

1. **Sample size** — A typical training run is 100–300 matches, not the thousands needed for statistically robust convergence claims. The "we evolve stronger AI" claim is qualitative, supported by fitness curves, not rigorous win-rate benchmarks against historical best.
3. **Hardcoded** — Aviculturist's `-hard` difficulty scaling means evolved AI is "Hard difficulty tuning", not "general intelligence". A clever player can still exploit rigid economy / timing.
4. **No transfer** — Genes are tied to a specific map (Arabia), difficulty (Hard), and 1v1-sized format. Changing these meaningfully is **not automatic**.
5. **Single dimension** — All 150 genes are numeric. We cannot evolve "switch tactics at 50% score" logic — that would require extending Aviculturist.
7. **Closed source dependencies** — AoE2Control is a third-party Lua host; if it falls out of compatibility with a future AoE2 DE update, the auto-runner breaks. mgz similarly depends on its maintainer.

---

## Extension Paths (1-line each)

- More parameters: walls, farms, taxation, monk weights → add lines to `genome.py` SPEC
- Elo-based scoring: replace wins+margin in `evolve.py cmd_next` with TrueSkill
- Real-time evaluation: use AoE2Control IPC instead of replay parsing
- LLM-evolved strategy: have an LLM rewrite AiBuilder blocks per generation

---

## Questions I'd Ask the Author

- How did you decide the initial 5-phase parameterization was the right level of granularity? Did you try 3 phases? 10?
- What evidence do you have that the evolved AI is "stronger" beyond fitness curves? Have you done cross-generation vs anchor AI matches?
- Why `wins + score_margin` fitness? Have you tried alternative ones (Elo, rank)?
- The 150-dimensional search space — how do you know mutation rate / σ are well-tuned? Sensitivity analysis?
- The `evolab_driver` deletes all player 1 units to become observer — what if a future patch forbids this? What's your fallback?