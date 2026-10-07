# 项目交接文档

> **目标读者**：接手这个项目继续做的人（含三个月后的你自己）
>
> **预计阅读**：20 分钟上手，2 小时可独立改基因/加新参数

## 0. 一句话总览

进化式《帝国时代 II》自定义 AI：把"AI 怎么做"拆成 150 个数（基因），用遗传算法迭代，让 AI 互打越打越强。

---

## 1. 上手 5 步

### 1.1 准备环境

- 操作系统：Windows 10/11（AoE2 决定版和 AoE2Control 都是 Windows-only）
- 游戏：《帝国时代 II：决定版》装好（Steam）
- Python：3.10+，建议用 venv 隔离
- 游戏内 `resources\_common\ai\` 目录可写（默认就 OK）

### 1.2 克隆与安装

```bash
git clone <repo-url> AoE2-AI-Arena
cd AoE2-AI-Arena
python -m venv .venv
.venv\Scripts\activate
pip install -r ai_lab/requirements.txt
```

### 1.3 配置

编辑根目录的 `config.json`，重点确认这几项：

```jsonc
{
  "game": {
    "install_dir": "C:\\Steam\\steamapps\\common\\AoE2DE",        // ← 你的游戏路径
    "recordings_dir": ""
  },
  "evolution": { "population": 8, "matches_per_ai": 3, "mutation_rate": 0.35, ... },
  "control": { "launcher": "tools/AoE2Control/AoE2Control.exe" }
}
```

> 💡 找不到录像目录？打开游戏打一局，看 `%USERPROFILE%\Games\Age of Empires 2 DE\<profile id>\savegame\` 下哪个子目录多了 `.aoe2record`。

`recordings_dir` 留空时，脚本会从 `%USERPROFILE%` 自动检查 `Games\Age of Empires 2 DE\<profile>\savegame` 及 `Documents\My Games\Age of Empires 2 DE\<profile>\savegame` 下的录像。若游戏使用其他位置，可在此填写具体录像目录。

### 1.4 第一次跑（半自动，2 分钟上手）

```bash
cd ai_lab
python make_ai.py --name Champion --mode default
python run_match.py Champion --vs-cpu hardest
```

游戏会启动，按屏幕提示建房（Arabia / Hard / 你设为观战者）。打完回到终端按回车自动生成 `lab_data/reports/<时间>_battle_report.md`。

### 1.5 进化第一代

```bash
python evolve.py init           # 生成第 0 代 8 个随机 AI + 打印对战表
python evolve.py report         # 每场打完记一笔
python evolve.py next           # 12 场全打完 → 进化出第 1 代
```

---

## 2. 项目架构（先看这张图）

```
┌─────────────────┐    写入 .per     ┌──────────────────────┐
│ ai_lab/genome.py│ ───────────────→  │ 游戏 AiBuilder 引擎  │
└─────────────────┘                   │ （5050行官方脚本）  │
       ↑ ↑ ↑                         └──────────────────────┘
       │ │ │                                  │
       │ │ │   渲染                            │ 执行
       │ │ └─ make_ai.py                       │
       │ └─ report.py ←── mgz 解析 ───────────┤
       └─ evolve.py  (init→next→champion)     │ .aoe2record 录像
                                             ↓
                                       ┌──────────────────────┐
                                       │ auto_runner.py       │
                                       │ 全自动跑局循环        │
                                       │（AoE2Control Headless）│
                                       └──────────────────────┘
```

**重要边界**：所有"AI 怎么做决策"的逻辑都来自游戏自带的 **AiBuilder**（`resources\_common\ai\AiBuilder.per`），我们只**调参数**（150 个 `phaseX-*` 常量），不写新规则。
**好处**：AI 永远能打（官方兜底），不会写出废脚本。
**代价**：基因的"表达力"受限于 AiBuilder 的参数面，想加"动态切换战术"这种逻辑得改 AiBuilder 本身。

---

## 3. 基因组怎么读

打开 `ai_lab/genome.py` 看 `SPEC` 列表。每行格式：

```python
("phase{n}-archer-cap-hard", 0, 30, [0, 4, 10, 16, 20]),
#  ↑ 参数名模板（{n}会被1-5替换）   ↑ 最小值  ↑ 最大值   ↑ 5 个相位默认值
```

按主题分组：

| 分组 | 参数 | 含义 |
|---|---|---|
| 经济 | `phase{n}-villager-cap-hard` | 各时代最多村民数 |
| 经济 | `phase{n}-food-gatherers` 等四个 | 食/木/金/石的采集配比（自动归一化到100） |
| 升时代 | `phase{n}-age-villager-requirement` | 多少村民后允许升时代 |
| 兵种构成 | `phase{n}-{单位}-cap-hard` | 各时代最多 X 个该单位（如 archer, knight） |
| 进攻行为 | `phase{n}-land-attack-percentage` | 多少比例兵力出击 |
| 进攻节奏 | `phase{n}-scale-attack-timer-hard` | 进攻间隔（值越小进攻越频繁） |

**为什么按 5 个相位（时代）展开**？
AiBuilder 把一场比赛分成 Phase 1~5，对应"封建→帝王中后期"等阶段。每个相位可以单独配置策略，实现"前期侦查→中期骚扰→后期大决战"的策略分层。

**为什么用 `-hard` 系列而不是 `moderate`？**
Aivizard 的兵种/村民数量参数按难度分支（easy/moderate/hard），Hard 才有进化意义。进化的就是 `*-hard` 这套常量。对局在 `config.json` 写 `Hard` 或以上。

---

## 4. 核心模块逻辑精讲

### 4.1 `make_ai.py` —— 基因→.per

**两个关键点**：

**(a) 替换算法**：用正则 `(defconst phase1-archer-cap-hard 0)` → `(defconst phase1-archer-cap-hard 12)`。精准替换，不破坏其他规则。

**(b) 注入相位推进规则**（`make_ai.py` 末尾的 `PHASE_RULES`）。这是**整个项目最关键的一笔**：

```lisp
(defrule
    (goal current-phase 1)
    (current-age >= feudal-age)
=>  (set-goal current-phase 2)    ; 时代变了就推进相位
    (chat-local-to-self "EvoLab: Phase 2 (Feudal)"))
```

Aivizard 的相位推进**默认靠场景 AI 信号**（剧情战役专用），单机对局永远停在 Phase 1。我们注入这 4 条规则让 AI 自动推进相位——否则帝王时代以后 AI 不知道切换到 Phase 4/5 的高级配置。

**(c) 注入基因 hash 播报**：

```lisp
(chat-to-all "EVOLAB A #f328d893")
```

录像里能直接看到跑的是哪个基因（防自动跑局改写 .per 时错位）。

### 4.2 `evolve.py` —— 遗传主控

子命令：

- `init`：生成种群（首个个体用官方默认风格作锚点）+ 装进游戏 + 打印赛程
- `report`：解析最新录像 + 记账到 `lab_data/results/gen_N.json`
- `next`：积分 → 选择 → 变异 → 装下一代
- `champion`：安装历史最佳为 `EvoAI_Champion`（替你打电脑用）

**适应度函数**（`cmd_next` 里）：

```python
fitness = (wins * 1.0) + (score_margin / 10000.0)
```

胜一场得 1 分，分差再加权（小数）。简单但够用——如果想更精细可以改成 Elo。

**锦标赛选择**（`_tournament`）：从种群随机抽 k=3 个，选胜者。比起轮盘赌更稳定。

### 4.3 `auto_runner.py` —— 全自动跑局

**原理**：

1. 调 AoE2Control Headless 模式注入游戏（不需要弹窗）
2. 部署 `evolab_driver.main.lua` 到 `%APPDATA%\CONTROL\AoE2Control\modules\`
3. 游戏里的 `evolab_driver` 模块（挂在玩家 1）自动：
   - 在主菜单时用 `DispatchStartGame()` 开局
   - `Init()` 里删除 P1 全部单位与建筑 → 变成纯观察者
   - `End()` 里检测到对局结束，**自动开下一局**（同配置）
4. Python 这边轮询录像目录，发现新 `.aoe2record` 就调 mgz 解析 → 写积分
5. 每局间隙 Python 改写 `EvoAI_A.per` / `EvoAI_B.per`（槽位名固定，换基因内容）

**关键设计**：**槽位名不变（永远 A/B），基因内容随局换**。这避免了每局都要重新配置大厅 AI 下拉框（一次性配置后永远不需再动）。

### 4.4 `evolab_driver.lua` —— 游戏内的"自动驾驶仪"

```lua
function Load(playerId)            -- 主菜单时：配置对局参数并开局
function Init()                    -- 比赛开始后：删除自己所有单位/建筑，变成观察者
function End(hasWon)               -- 比赛结束时：自动开下一局（DispatchStartGame）
```

**坑点记录**：
- `Dispatch*` 系列在 Tournament Mode / Multithreading Mode 开启时会被拒绝 → 模块启动后菜单里这两个开关必须保持关闭
- 删除单位用 `DeleteUnit(obj)`，要包 `pcall` 因为单位可能在删除瞬间已经无效

---

## 5. 扩展方向（按优先级）

| 想做什么 | 改哪里 | 难度 |
|---|---|---|
| 加新参数（比如"什么时候起双 TC"） | `genome.py` 的 `SPEC` + `AiBuilder.per` 加对应 `defconst` | ⭐ |
| 换地图（不只 Arabia）| `evolab_driver.lua` 的 `CFG.map`，或加随机地图池 | ⭐ |
| 加 Elo 评分（避免代数间公平性差异）| `evolve.py` 的 `cmd_next`，用 `TrueSkill` 包 | ⭐⭐ |
| 接入 IPC（实时评估而非事后看录像）| `evolab_driver.lua` 加 IPC server + Python client | ⭐⭐ |
| 让基因影响战术选择（而不只是数值）| 改 `AiBuilder.per` 加新规则块 | ⭐⭐⭐ |
| 训练 LLM 写 .per（策略级进化）| 新增 `llm_evolve.py`，每代用 LLM 改写规则 | ⭐⭐⭐ |

---

## 6. 常见问题排查

| 现象 | 原因 | 修复 |
|---|---|---|
| 聊天栏看不到 `EVOLAB A #...` 播报 | 游戏加载 .per 失败（语法错）| 看 `AiBuilder.per` 是否有未对齐括号；加 `print(re.search)` debug |
| AI 不动 / 卡在黑暗时代 | 相位推进规则没注入 | 确认 `make_ai.py` 的 `PHASE_RULES` 加到了 .per 末尾 |
| `python evolve.py report` 报"没有最新录像" | 游戏录像路径与 `config.json` 不一致 | 检查 `recordings_dir` |
| `auto_runner.py` 跑不出录像但游戏在对战 | `recordings_dir` 路径错 或 单人录像存到另一目录 | 用 `find /c/Users/fengy/Games -name "*.aoe2record" -newer <timestamp>` 排查 |
| mgz 解析胜者为 None | 录像没有 postgame 区段 | 改用分数定胜负（`report.py` 已加 fallback）|
| AoE2Control Headless 退出码 6/7 | 游戏没起来/最小化 | 保持游戏窗口正常显示 |
| 进化很多代但个体没变化 | 突变率太低 | `config.json` 的 `mutation_rate` 调到 0.5+ |

---

## 7. 关键文件速查表

| 想看... | 文件 |
|---|---|
| 基因有哪些参数 | `ai_lab/genome.py` |
| 怎么把基因变成游戏脚本 | `ai_lab/make_ai.py` |
| 遗传算法怎么算适应度 | `ai_lab/evolve.py` 的 `cmd_next` |
| 自动跑局怎么驱动游戏 | `ai_lab/auto_runner.py` + `ai_lab/control/evolab_driver/evolab_driver.main.lua` |
| 战报怎么解析录像 | `ai_lab/report.py` |
| 配置文件 | `config.json` |
| 面试自我介绍怎么说 | [docs/interview-self.md](interview-self.md) |
| 给面试官的项目介绍 | [docs/interview-reviewer.md](interview-reviewer.md) |

---

## 8. 项目进度记录

- 2026-10-05：搭好骨架，半自动跑通（第 0 代 8 个随机 AI → 第 1 代）
- 2026-10-06：接入 AoE2Control，全自动跑通

详见 `lab_data/` 下的存档（不进 git）。
