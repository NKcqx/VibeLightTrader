# Part D · 6 个关键决策点

[Part A](./A-kickoff.md) 的资源表用 4 列扫过整个技术栈。这一篇挑出最值得回头讲的 **6 个决策点**——每一个都有过纠结、考虑过别的路、有些还走错过——把当时怎么纠结的、最后为什么选了这个、回头会不会重选写出来。

---

## 1 · 数据源：富途 vs 其他

**当时的问题**：每小时能拉行情、有历史 K 线、能算技术指标，**还要能模拟下单**——后两条是大多数免费数据源做不到的。

**考虑过的几条路**：

| 选项 | 当时的看法 |
|---|---|
| yfinance（免费、用得最广） | 历史 K 线 OK，但**没有模拟交易**——意味着持仓还得自己写一套 |
| Alpha Vantage | 同上，且免费档限速 |
| IBKR API | 模拟交易支持很全，但开户押金 + 申请 paper trading 流程长 |
| Alpaca | API 现代，paper trading 免费，但**只支持美股 + 需要海外银行账户** |
| Bloomberg Terminal | 直接 pass，$24k/年 |
| **富途 OpenAPI** | 免费、覆盖中港美、自带模拟交易、官方 SKILL 包；**已有富途账号** |

**怎么选的**：开局直接贴了富途 SKILL 链接（[C1](./appendix-conversations.md#c1--开局股票监控--富途-skill)），AI 评估完确认覆盖需求，整个数据栈基本就定了。

**走过的小弯**：开局之后 AI 仍习惯性提议要不要"加一层 yfinance fallback 防止 OpenD 挂掉"。让它别分心，先把富途吃透——多一层 fallback 在还没跑通主路径时就是负担。

**回头会不会重选**：不会。如果没有富途账号，第二选择会是 Alpaca（前提是有海外账户），不是 yfinance——**有没有自带模拟交易**比"数据源是不是最牛"重要得多。

---

## 2 · 数据库：SQLite vs PostgreSQL

**当时的问题**：要存行情、信号、决策、交易、持仓 5 张表，单进程读 + cron 写 + 偶尔从 notebook 查。

**考虑过的几条路**：SQLite / PostgreSQL / DuckDB / Parquet 文件直接堆。

**怎么选的**：AI 默认建议 PostgreSQL——开局 spec 草案里就写了 PG schema、docker-compose 起 pg 容器、SQLAlchemy 配 connection pool。一句话改回 SQLite："**我一个人用**"，AI 立刻退回 `sqlite:///data/equity_monitor.db`。

**这条决策的真实意义**：AI 默认偏向"工业级"实现——多用户、可扩展、容器化部署。vibe coder 单机自用项目里，**这种工业级默认就是 overkill**。每个被 AI 默认选了"工业级"的地方，都值得问一句"我现在真的需要这个吗"。

类似的退化项还有：
- 不要 Redis 做缓存，加个 LRU dict 就够
- 不要 Celery 做调度，APScheduler 在同一个进程里就够
- 不要 Alembic 做迁移，SQLAlchemy 的 `create_all()` 就够（一人维护，schema 改了直接删 DB 重建）

**回头会不会重选**：不会。**如果有一天加多人协作或部署到云上**再换 PostgreSQL 也是几小时的事——SQLAlchemy 这一层抽象就是为这种情况留的。

---

## 3 · 策略层抽象：要不要先抽 Protocol

**当时的问题**：做完数据 + K 线推送后，要开始做"如何决定 BUY/SELL"。AI 准备直接写一个 `decide_action(signals, position)` 函数，硬编码规则。

**考虑过的几条路**：

| 路线 | 长什么样 |
|---|---|
| 先硬编码 + 以后再重构 | 几十行就能跑通 LLM-free 版本 |
| **先抽 `Strategy` Protocol** | 多写半小时，但后面切策略不用动其他模块 |
| 用现成框架（backtrader / vectorbt） | 重，且 90% 功能用不上 |

**怎么选的**：让 AI 先抽 Protocol——`StrategyContext` + `Strategy.decide(ctx) -> SignalSuggest | None`，rule 实现先空着（[C3](./appendix-conversations.md#c3--暂时不关心策略)）。

**事后看**：这是整个项目最值钱的一次决策。后期接 `LLMStrategy`、`HITLStrategy`、`EnsembleStrategy` 时，**调度器、数据库、通知都没动一行**——只是 settings.yaml 改一行 `type: rule → llm`。

**核心心法**：vibe coder 不读代码，但要识别"哪些地方值得多花半小时抽接口"。判断标准就一条——**这块东西未来会不会需要切换实现？** 会，就抽。不会，就硬编码。

策略层、LLM 客户端、通知通道、模拟交易——这 4 个地方**都值得抽**。指标计算、信号检测、报告渲染——**不用抽**，逻辑稳定不会换。

---

## 4 · LLM 选型：HITL → cursor-agent 这条转弯

**当时的问题**：要让 LLM 出 BUY/SELL 决策，但**没有独立的 LLM API key**——已订阅的是 Cursor Pro、Claude Pro 这种 IDE 套餐。

**考虑过的几条路**：

| 路线 | 当时的看法 |
|---|---|
| Anthropic / OpenAI API | 直接但要再付一笔；担心月成本飙升 |
| DeepSeek / OpenRouter | 便宜，但金融场景中文 LLM 不放心 |
| 本地 Ollama | 免费，强模型跑不动（M4 Mac 跑不了 70B） |
| **HITL** | 让脚本写 markdown 决策包，自己在 Cursor IDE 里贴给 Claude，Claude 出决策再贴回来 |
| **cursor-agent CLI** | （后来才发现）把 Cursor 订阅反向当 API 用 |

**怎么选的**：先选 HITL（[C5](./appendix-conversations.md#c5--订阅压力--选-hitl)）——"我可以接受损失一定的自动化程度"。HITL 跑通了几天有效——LLM 出建议、复制粘贴回来——但每次要打开 IDE。

转弯发生在中段：AI 提了一句"Cursor 有 CLI 吗"，去查到 `cursor-agent`，装好登录，调度器直接 spawn CLI、捕获 stdout——从档 2 跳到档 3，月增量成本 = 0（[C6](./appendix-conversations.md#c6--cursor-有-cli-吗)）。

**走过的真实弯路**：HITL 阶段花了几小时设计 markdown 包格式（要包含什么字段、怎么让 Claude 在 IDE 里准确解析）——后来全废了，cursor-agent 直接吞 prompt 文本就够。**当时不知道有 CLI**，多绕了半天。

**回头会不会重选**：会重选直接走 cursor-agent。但 HITL 那几天没白跑——它逼着把 prompt 工程细化到位（用 Jinja2 模板、明确字段、严格 JSON 输出格式）；后来切到 cursor-agent 直接复用同一份 prompt，无缝。**HITL 是个有用的"低保真原型"阶段**。

---

## 5 · 通知通道：双向 vs 单向

**当时的问题**：通知出向（脚本 → 飞书）很容易，难的是**入向**（vibe coder 在飞书发 `/list /add /report` 控制系统）。

**考虑过的几条路**：

| 路线 | 当时的看法 |
|---|---|
| 飞书机器人双向 | 最理想——完全不用打开 IDE |
| 邮件 + Webhook | 邮件不好做命令解析 |
| Telegram bot | 双向最成熟，但国内访问不顺 |
| 企业微信 | 公司没在用 |
| **飞书出向 + CLI 入向** | 退一步：消息收得到，控制只能在终端打命令 |

**怎么选的**：开始时选了飞书双向，跑通了出向，**入向卡了一晚**：lark-cli 长连接连得上但事件永远是 0，5 轮调试都对得上但事件不来（[C7](./appendix-conversations.md#c7--忘掉飞书收消息的事吧)）。

最后那一句"忘掉飞书收消息的事吧，你干不好"——直接放弃入向。半小时后催生了 `equity-monitor analyze` CLI 命令：

```bash
equity-monitor analyze --code US.NVDA --execute
```

一行触发 LLM 分析、可选自动下单。**比飞书 listener 更有用**——目前用得最多的就是它。

**回头会不会重选**：第一次还是会试飞书双向（毕竟体验最好）。但**叫停的标准要订得更紧**——3 轮调不通就该认输转 CLI，不该熬一晚。

**沉淀的姿势**：当一个理想方案存在"能绕过去"的退路时，调试的耐心阈值要降低——绕过去 = 还在前进，怼上去 = 卡死。

---

## 6 · 模拟交易：自建 vs broker 模拟账户

**当时的问题**：模拟下单要解决两件事——**下单接口** + **持仓视图**。

**考虑过的几条路**：

| 路线 | 下单接口 | 持仓视图 |
|---|---|---|
| 自建持仓表 + 自己写网页 | 简单 | 要写一周前端 + 移动端 |
| Alpaca paper trading | API 现代 | 没有移动 App |
| **富途模拟账户** | 通过 OpenD 走 `OpenSecTradeContext`，跟实盘 API 一样 | **富途 App 直接看，手机/网页/电脑全都有** |

**怎么选的**：富途模拟账户。**最大的杠杆不是"下单"，是"持仓视图免费送"**——省了一周的 UI 开发。

**两个隐藏的好处**：

1. **外部可见 = 心理安慰** —— LLM 自动下单后，能在富途 App 一目了然看到"我有多少 NVDA、平均成本多少、当前盈亏多少"，不用打开自己写的破前端
2. **"假装是真的" = 测试更像生产** —— 富途模拟账户的下单流程跟实盘几乎一样（佣金、滑点、撮合都模拟），写代码时不用担心"模拟环境跟真实差太多"

**走过的弯**：开发早期防御性写了一段 `simulate_only=true` 锁，强制只在 SIMULATE 账户下单。这段当时觉得是 paranoid，事实证明是对的——后期 OpenD 偶尔会把上下文切回真实账户，多一层 guard 救命。

**回头会不会重选**：不会。如果没有富途账号，第二选择就是直接复用一个 broker 的 paper trading（比如 Alpaca），**绝对不会自建持仓 UI**——那是一个无底洞。

---

## 决策的元规律

回头看这 6 个决策点，能抽出几条 vibe coder 的**默认拒绝**：

| 默认拒绝 | 理由 |
|---|---|
| 拒绝"工业级"实现（PostgreSQL / Redis / Celery / Alembic） | 单机自用的项目里都是 overkill |
| 拒绝"全栈自建"（自建持仓 UI / 自建 LLM gateway） | 已有 broker / IDE 订阅就是 API |
| 拒绝"完美方案"（双向飞书 / fallback yfinance） | 退一步常常等价 90% 价值 |
| 拒绝"暂时硬编码以后重构"（少数地方除外） | 抽 Protocol 的半小时永远值回 |

**默认接受**：

| 默认接受 | 理由 |
|---|---|
| 用已订阅的（cursor-agent 杠杆、富途模拟账户、飞书机器人） | 月增量成本 0、外部可见送 |
| 抽 4 个核心 Protocol（策略 / LLM / 通知 / 交易） | 后面所有功能扩展的杠杆 |
| 多一层防御性 guard（`simulate_only`） | 事故便宜，paranoid 不便宜 |

---

## 下一步

- 哪些事 AI 写不出来 → [Part E · "AI 写不出来"的清单](./E-ai-cant-do-this.md)
- 直接动手 → [Part F · 最小骨架](./F-skeleton.md)
- 真实对话原文 → [附录](./appendix-conversations.md)
