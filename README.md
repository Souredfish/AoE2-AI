# AoE2 AI Arena

> 演化计算 + 游戏 AI 实战：用遗传算法迭代出能打赢互相的《帝国时代 II：决定版》自定义 AI。

---

## 📑 文档总入口

根据你的身份，挑对应的入口：

| 你是谁 | 看这里 | 用途 |
|---|---|---|
| **接手这个项目继续做的人** | **[docs/handover.md](docs/handover.md)** | 项目架构、运行步骤、扩展方向、新人上手顺序 |
| **我自己做面试自我介绍** | **[docs/interview-self.md](docs/interview-self.md)** | 2 分钟电梯版讲解 + 10 分钟深度版讲解 + 常见追问预案 |
| **面试官快速了解项目** | **[docs/interview-reviewer.md](docs/interview-reviewer.md)** | 1 分钟摘要 + 技术栈 + 项目结论与局限 |

技术细节、生产代码、命令清单看下方章节即可。

---

## 这是什么

让两个 AoE2 自定义 AI 在游戏里对打，遗传算法根据胜负进化参数，几代之后产出越来越强的 AI。
- **替你打电脑**：本地起游戏，AI 互搏，你当观察者
- **AI 互相打 + 战报**：每场结束自动解析录像，输出胜者/时长/分数
- **全自动跑代**：AoE2Control Headless 注入，24h 无人值守跑完一整代 12 场

## 关键数据

- **基因面**：150 个参数（村民封顶、四资源配比、兵种构成、进攻频率），按 5 个时代相位分层
- **底座引擎**：游戏自带的微软官方 AiBuilder 框架，5050 行 .per 规则，零裸写
- **进化算子**：精英保留 + 锦标赛选择 + 均匀交叉 + 高斯变异
- **对接**：mgz 库解析 .aoe2record 录像；AoE2Control 提供 Headless 建房 API

## 一图流（进化闭环）

```
evolve.py init
   ↓ 生成 8 个随机基因，注入游戏
auto_runner.py  ──────→  游戏内 evolab_driver（Lua）：
  改写 EvoAI_A/B.per      自动建房 → P1 自毁观察 → 结束自动重开
  等新录像                          ↕
  mgz 解析 → 积分账本        对局双方执行 .per
   ↓
evolve.py next
   ↓ 精英保留 + 锦标赛 + 交叉 + 变异
第 N+1 代
```

## 仓库结构

```
AoE2-AI-Arena/
├── README.md                              ← 你在这里（总入口）
├── docs/
│   ├── handover.md                        ← 项目交接
│   ├── interview-self.md                  ← 面试自我讲解
│   └── interview-reviewer.md              ← 面试官速览
├── ai_lab/
│   ├── genome.py          # 基因组定义（150 参数）
│   ├── make_ai.py         # 基因 → 游戏 .per/.ai 文件
│   ├── run_match.py       # 手动跑一场对战
│   ├── auto_runner.py     # 全自动跑代（AoE2Control 驱动）
│   ├── report.py          # .aoe2record 录像解析
│   ├── evolve.py          # 遗传算法主控（init/report/next/champion）
│   ├── requirements.txt
│   └── control/
│       └── evolab_driver/evolab_driver.main.lua   # 游戏内自动驾驶
├── config.json            # 全局配置（路径/赛程/进化参数）
└── .gitignore             # 排除运行时缓存与第三方二进制
```

## 快速自检

```bash
# 装依赖
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r ai_lab/requirements.txt

# 半自动：装一个默认风格 AI，启动游戏，按提示建房
python ai_lab/make_ai.py --name Champion --mode default
python ai_lab/run_match.py Champion --vs-cpu hardest

# 全自动：详见 docs/handover.md 的「全自动模式」章节
```

## 致谢

- AoE2 决定版官方 AiBuilder 框架（`resources\_common\ai\AiBuilder.per`）
- [AoE2Control](https://aoe2control.github.io/) — 1.1.0
- [mgz](https://pypi.org/project/mgz/) — 录像解析