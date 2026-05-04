# Part A · 立项与心智设定

## 项目宪法是边走边长出来的

最初提交给 AI 的需求是这样（[原话见 C1](./appendix-conversations.md#c1--开局股票监控--富途-skill)）：

> 做一个股票监控和模拟操盘的功能，能每小时监控指定标的、获取历史价格、计算 RSI/MACD 等技术指标、做技术面分析；最好还能做基本面（公司新闻、市场情绪、机构评级）。推荐用富途牛牛的 SKILL。

里面没写金额、周期、标的、LLM 自动下单、回撤容忍——这些都是中段补的：

- "5 万美金 / 标的、上下浮动 20%" 在做策略前才提
- "NVDA + MSFT 两只" 是为减少并行复杂度才框死的
- 投资者画像 21 个字段，是 LLM 满仓买完才反应过来需要的（[C8](./appendix-conversations.md#c8--你的优势就是中长线)）

每次 AI 输出让人不踏实，那一刻就多出了一条隐性偏好——把它说出来，加进配置 / spec / prompt。项目的"宪法"是这样长出来的，不是开局就完整。**唯一别做的是用"做个量化系统"这种大词蒙混**，AI 听到大词只会还一个大而无当的脚手架。

---

## 资源选型是 vibe coder 真正动脑子的地方

vibe coder 不写代码，但选型这一关绕不开——AI 知道有哪些选项，但**它不知道你已经有什么**。

把开局的资源清单贴给 AI（[C1](./appendix-conversations.md#c1--开局股票监控--富途-skill)），AI 几分钟就能评估完。下面这张表是这个项目最后落定的栈：

| 资源 | 我的需求 | 可选方案 | 最终选择 & 原因 |
|---|---|---|---|
| **行情数据** | 实时报价 + 历史 K 线 + RSI/MACD 指标 + **要能模拟下单** | yfinance / Alpha Vantage / IBKR API / Alpaca / Bloomberg / **富途 OpenAPI** | 富途 OpenAPI（OpenD）。**已有富途账号**，免费、覆盖中港美、自带模拟账户能在富途 App 直接看持仓——省了一大块持仓 UI 的活 |
| **LLM 推理** | 出 BUY/SELL 决策；**月增量成本 ≈ 0**；中长线推理深度要够 | Anthropic API / OpenAI API（贵）/ DeepSeek（便宜但金融弱）/ Ollama 本地（强模型跑不动）/ **cursor-agent CLI**（小众）/ Claude Code SDK（同思路） | cursor-agent CLI——把 Cursor Pro 订阅反向当 API 用。这是个**小众选择**：CLI 启动慢（30–60 s）、不能控温度、并发受限。但单符号决策一天就几次，慢一点无所谓；订阅已付，月增量 = 0。先走过 HITL（[C5](./appendix-conversations.md#c5--订阅压力--选-hitl)），AI 提了一句 CLI（[C6](./appendix-conversations.md#c6--cursor-有-cli-吗)），跳档自动化 |
| **通知** | 手机能收 + 富文本卡片 + 公司就在用 | 邮件 / Slack / Telegram / 微信公众号 / **飞书机器人** | 飞书 + lark-cli。公司已用、官方 CLI 完整、卡片漂亮。原本想做双向（飞书发命令控制系统）卡了一晚没成（[C7](./appendix-conversations.md#c7--忘掉飞书收消息的事吧)），最后用 CLI 命令替代 |
| **模拟账户** | 能下单、能查持仓，**最好不用自己写 UI** | 自建持仓表 + 写网页 / Alpaca paper / **富途模拟账户** | 富途模拟账户。复用富途账号、富途 App 直接看持仓和盈亏——**省了一周 UI 开发** |
| **数据库** | 单机、零运维、跨进程读 | SQLite / PostgreSQL / DuckDB / Parquet | SQLite。AI 默认建议 PostgreSQL，被改回 |
| **Python 环境** | 隔离、能跑 numpy/pandas | venv / **conda** / uv / poetry | conda env `fin`。本机已有 conda；AI 几次自作主张装 uv 都被打断（[C2](./appendix-conversations.md#c2--你怎么又用到-uv)）|
| **Coding agent** | 在 IDE 里跟 LLM 协作 | Cursor / Claude Code / Codex CLI / Aider | Cursor。已订阅，且后面才发现可以反向接 cursor-agent，一笔订阅当两笔用 |

可借鉴的几个姿势：

1. 先列已有的，再问 AI 够不够用——比让 AI 从空白处推荐快一个量级
2. 小众路径常常更划算——cursor-agent 网上几乎没人写，但因为已经付过 Cursor 订阅，它就是这个项目最大的成本杠杆
3. 先选一个跑通再优化——富途、SQLite、conda 都不是"最优"，是"最近"

这张表是回头复盘的样子。当时并没有这么清醒地比较过——很多决策是 AI 列了 3 条直接挑的，事后证明对了；也挑错过又改回来（数据库一开始让 AI 上了 PG schema，跑了一圈才退回 SQLite）。这张表的价值不是"照着选"，是**告诉你哪些维度需要 vibe coder 自己拍板**。

---

## 自动化档位：先低后高

让 AI 替你做多少，决定项目最终长什么样。这个项目走了三档：

| 档位 | AI 做到哪一步 | 你做什么 |
|---|---|---|
| 1 — 只盯盘 | 拉行情、算指标、推送 K 线图 + 信号到飞书 | 自己看图、自己下单 |
| 2 — 出建议 | 上一档 + LLM 给 BUY/SELL/HOLD 建议 + 一段理由 | 看完建议自己点确认下单 |
| 3 — 自动执行 | 上一档 + 直接下单到模拟账户 | 看消息（出问题再叫停） |

实际路径是 1 → 2（HITL：LLM 在 Cursor IDE 里出建议，复制粘贴回脚本，[C5](./appendix-conversations.md#c5--订阅压力--选-hitl)）→ 3（cursor-agent CLI 接管，[C6](./appendix-conversations.md#c6--cursor-有-cli-吗)）。每档都跑通几天才升级。

直接奔档 3 的风险不在技术，而在**没给自己留时间确认 LLM 判断的质量**。第一次看到 LLM 满仓 251 股 NVDA 的 BUY，第一反应是不踏实——那种不踏实只能在档 2 建立信心后才会消失（之后就有了 [C8](./appendix-conversations.md#c8--你的优势就是中长线) 那次对话）。

---

## 角色边界

| 我做的 | AI 做的 |
|---|---|
| 决定一句话目标、改 spec、否决新功能 | 把目标翻成代码、写 spec、列方案 |
| 选型时拍板（特别是用已有资源） | 列 trade-off |
| 把不踏实变成显性配置 | 写 prompt 和错误处理 |
| 知道飞书后台 / 富途 App / 本机 cwd 长什么样 | 假设标准环境，看不到你看到的 |

---

## 下一步

- 项目怎么一步步长出来的 → [Part C · 项目是怎么长出来的](./C-evolution-timeline.md)
- 直接动手 → [Part F · 最小骨架](./F-skeleton.md)
- 协作中卡住 → [Part B · 7 个 meta-skill](./B-meta-skills.md)
- 真实对话原文 → [附录 · 对话精简版](./appendix-conversations.md)
