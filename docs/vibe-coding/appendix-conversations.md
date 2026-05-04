# 附录 · 真实对话精简版

正文里引用过的几段真实对话，按出现顺序排号。每条都做了精简，保留原话框架。

---

## C1 · 开局：股票监控 + 富途 SKILL

**我：**
> 我们现在来做一个股票监控和模拟操盘的功能。能实时（每个小时）监控指定标的价格、获取历史价格、获取各种技术指标（RSI、MACD）、并进行技术面分析；最好还能进一步获取基本面信息，如公司新闻、市场情绪、机构评级等。你看下都需要如何开展，另外我推荐这个富途牛牛的 SKILL：`https://www.futunn.com/skills/futu-install.md`，你看下能否用上。

**AI：** 评估了富途 SKILL 的 API 覆盖面，输出 `docs/spec.md` 草案，里程碑 M1–M7。

**落地：** 富途 OpenAPI 直接进入栈；spec 出来后 vibe coder 只读 spec，不读代码。

---

## C2 · 你怎么又用到 uv

**我：**
> 暂停一下，你怎么又用到 uv 了？我不是说这个和 conda 冲突，要复用我们这个项目专门创建的 "fin" conda 环境吗。

**AI：** 撤回 uv 相关改动，回到 conda env `fin`。

**落地：** "全程使用 conda env `fin`，不要装 uv" 写进 system prompt，类似中断后续没再发生。

---

## C3 · 暂时不关心策略

**我：**
> 我暂时不关心策略，我只想先把架子搭好，例如查看 K 线图、和历史操作记录、收益率等等，我们后期会专门再尝试不同策略的。

**AI：** 把策略层抽成 `Strategy` Protocol，留空实现，先做数据 / 调度 / 报告 / 模拟交易。

**落地：** 后期接入 rule / llm / hitl / ensemble 时各模块零修改。

---

## C4 · 卡片太简陋我看不懂

**我：**
> 你发送的卡片内容太简略，"穿越上限阈值 (close=198.45, upper=150.0)"、"RSI 超买 (rsi=71.35733884186854)" 我不理解什么意思，所以每次说完信号特征以后解释下意味着什么。

**AI：** 新增 `reports/interpret.py`，把每个信号 kind 翻译成一句白话（如"短期超买，回调风险升高，但不是 SELL 信号"）。

**落地：** 信号卡片里每条信号都附了一句白话解读。

---

## C5 · 订阅压力 / 选 HITL

**我：**
> LLM Key 是个关键问题，现在的问题是我买的都是订阅制的软件，例如 Cursor、Codex、Claude，没有能直接接入的 LLM API Key，能利用这些软件里的 LLM 解决问题不然太浪费了，我可以接受损失一定的自动化程度。
> 我想先做 D，验证有效后再接入 A；记住 D 方案里其实是你自己和自己对话，所以你能最精确高效的编写 prompt 和上下文。

**AI：** D 方案 = HITL（Human-in-the-Loop）：脚本写一份 markdown 决策包到 `var/decisions/`，vibe coder 在 Cursor IDE 里把这份包贴给 Claude，Claude 出 BUY/SELL 决策，再贴回 `equity-monitor decide submit` 回填。

**落地：** `strategy.type = hitl` 跑了几天。验证有效后才寻找下一档。

---

## C6 · cursor 有 CLI 吗

**我：**
> cursor 有 CLI 吗？有的话我就不用在这个 IDE 里对话了吧，直接写进脚本里是不是就完全自动化了。
>
> （查到 cursor-agent 后）登录了，返回值：`✓ Logged in as ...`，往后一口气推进。

**AI：** 接入 `cursor-agent` CLI，调用 Cursor 订阅里的 Opus；写 `OpenAICompatClient` / `AnthropicClient` / `CursorAgentClient` 三个实现，由 `LLMClient` Protocol 统一调度。

**落地：** 自动化从 HITL 跳到全自动；月增量 LLM 成本 = 0。

---

## C7 · 忘掉飞书收消息的事吧

**我：**
> 这个功能你已经做了很久了，暂时放弃吧。
> 忘掉飞书收消息的事吧，你干不好。现在主动触发一次操盘分析，用 cursor agent 分析当前几个标的在我给出的投资周期和总投资金额下处于什么情况，该不该 BUY/SELL。

**AI：** 停止调试 lark-cli 长连接；新增 `equity-monitor analyze --code US.NVDA --execute` 命令，按需触发 LLM 分析 + 可选自动下单。

**落地：** 飞书双向交互能力放弃；analyze CLI 成为日常使用最频繁的命令。

---

## C8 · 你的优势就是中长线

**我：**
> 你一直没做吗？你的优势就是中长线，除了我刚说的这些信息外，要做好中长线投资你还需要维护其他配置吗？先列进去，然后再给出几个推荐配置套餐，最后单独总结成一个文档。

**AI：** 在 `config.py` 增 `InvestmentProfileConfig`（21 个字段：horizon、budget、风险容忍、加仓节奏、止盈止损、最短持有天数等），写进每次 LLM prompt；同步保守 / 平衡 / 进取 三套推荐套餐，单独整理为 `docs/mid-term-investing.md`。

**落地：** 下一次 LLM 出 BUY 时理由是"按预算四成入场，剩余分两次加仓"——不再满仓梭哈。

---

如有补充，可以直接对照 `docs/dev_log.md` 与 `data/journal/<code>.md` 还原更细粒度的现场。
