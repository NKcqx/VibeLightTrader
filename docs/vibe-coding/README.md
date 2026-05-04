# Vibe Coder 复刻指南

写给"用 Cursor / Claude Code / Codex 这类 coding agent 协作、自己不一定写代码、想从 0 复刻一个类似项目"的人。

技术实现写在 [`../tutorial/`](../tutorial/)（待出）；这份文档讲的是**过程**——`equity-monitor` 这个项目是怎么从一句话需求长到今天的，以及复刻一个类似的可以怎么走。

---

## 目录

| Part | 篇名 | 看完会知道 |
|---|---|---|
| [A](./A-kickoff.md) | 立项与心智设定 | 一句话目标怎么写、资源怎么扔给 AI、自动化档位别一上来就最高 |
| [B](./B-meta-skills.md) | 7 个 meta-skill | 不说就会出问题的 7 个动作（含飞书 listener 真实卡点） |
| [C](./C-evolution-timeline.md) | 项目是怎么长出来的 | 8 个真实转折，每个标记是谁拍板的、当下踏不踏实 |
| [D](./D-decision-points.md) | 6 个关键决策点 | 数据源 / 数据库 / 策略抽象 / LLM 选型 / 通知 / 模拟交易 — 每处展开纠结 |
| [E](./E-ai-cant-do-this.md) | "AI 写不出来"的清单 | 必须自己干的 6 类事——thesis / OAuth / 订阅 / 装机 / 不踏实 / 收尾 |
| [F](./F-skeleton.md) | 最小骨架 + 30 行 demo | 直接复制走、跟你自己的 AI 一起填肉的起点 |
| [附录](./appendix-conversations.md) | 对话精简版 | 正文里引用过的 8 段真实对话原文 |

读法：

- 想做新项目 → A → F，中途穿插 B
- 已经在做、卡住了 → B + E
- 想了解代码怎么写 → 等 `../tutorial/`

---

## 为什么是这个项目

`equity-monitor` —— 中长线美股监控 + 模拟交易：

- 数据：富途 OpenD（免费）
- 调度：每小时拉行情、算 RSI/MACD/Boll、检测信号
- 决策：用 LLM（cursor-agent）按"投资者画像"出 BUY/SELL/HOLD
- 执行：模拟下单到富途 SIMULATE 账户（手机/网页都能看持仓）
- 通知：飞书卡片
- 观测：每标的一份 Markdown journal + 决策审计日志

复刻价值高的几个点：

1. 零 LLM API 成本 —— 用 cursor-agent 复用 IDE 订阅
2. 零量化数据成本 —— 富途免费 API
3. 持仓外部可见 —— 模拟账户在富途 App 直接看
4. 配置驱动 + Protocol 抽象 —— 换数据源 / 换 LLM / 换通知 = 改一行 yaml
5. 可循序渐进 —— 自动化档位 1 → 2 → 3 各跑得通

---

## 资源最低版

| 类型 | 必需 | 替代品 |
|---|---|---|
| Coding agent | Cursor（推荐） | Claude Code / Codex CLI / Aider |
| LLM 订阅 | Cursor Pro/Max | Claude Pro / OpenAI Plus / 自有 API key |
| 数据源 | 富途 OpenD（自带模拟账户）| Yahoo / IBKR / Bloomberg |
| 通知通道 | 飞书机器人 | 邮件 / Slack / 微信公众号 |
| Python 环境 | 3.11+ | 3.10 也能跑 |

至少 3 项已具备，复刻成本就很低。
