# Part C · 项目是怎么长出来的

> 这一篇按时间顺序记录 8 个真实转折，每个都标出**vibe coder 在那一刻拍板了什么**。
> 引用了的对话原文都在 [附录](./appendix-conversations.md) 里，链接随处可点。

---

## 1 · 开局那一句话

开局信息一并贴了富途牛牛的官方 SKILL 链接（[C1](./appendix-conversations.md#c1--开局股票监控--富途-skill)）。AI 几分钟评估完，整个数据栈定下来。

很多 vibe coder 一开始会问"做股票盯盘有哪些方案"——AI 会列十几条 trade-off，然后人就淹死在选型里。把**已经有的资源**先扔过去让 AI 评估"能不能用上"，效率高一个量级。

> ✅ 开局之前列一份资源清单（订阅、账号、内部工具、SKILL 包），一次性扔过去；后面对话里 AI 也会优先在这堆里挑实现。

---

## 2 · "spec 审核结果？"

AI 拉通富途 SKILL 后写了 `docs/spec.md`——里程碑 M1–M7、模块拆分、Protocol 草稿。改 spec、不改代码：

- M3 K 线图原本想"每天画 HTML"——改成"找现成工具能交互、能切频率"，落到 mplfinance + 飞书图片消息
- 数据库 AI 建议 PostgreSQL——改成 SQLite，理由就一句"我一个人用"
- 策略层 AI 准备硬编码——让它先抽 Protocol（[C3](./appendix-conversations.md#c3--暂时不关心策略)）

> ✅ spec 是 vibe coder 唯一会改的"代码"。改完 AI 会按 spec 写实现，**写错也是按 spec 写错**——这反而好排查。

---

## 3 · 装环境那个下午

最早扎人的不是写代码，是装东西。中段打断过几次 AI："你怎么用 uv？我用 conda"（[C2](./appendix-conversations.md#c2--你怎么又用到-uv)）；"env 名字太长，改 `fin`"；"OpenD 装好了吗，11111 端口看一下能力"。

AI 的默认环境假设几乎从不跟你的本机一样。每次"AI 跑得通你跑不通"的根都在这。

后来形成的反应：

```
我用的是 conda env "fin"，python 在 /opt/.../envs/fin/bin/python3。
后续所有命令、测试、安装都在这个 env 里。
```

塞进 system prompt，类似中断后续就少了很多。

---

## 4 · 第一个反馈环

行情、SQLite、K 线图、飞书图片消息——大概第三天，第一次拿到完整反馈环：手机收到一张 NVDA 的 60 分钟 K 线。

那一刻没有接 LLM、没有自动下单、没有策略，但已经比手动看盘强很多。立刻 commit。

> ✅ 反馈环越早拿到越好。**第一个能让你说"这就够了"的版本**，永远比第一个"完整"的版本早 5 天。中间那 5 天是建立项目信心的时间。

---

## 5 · 那张飞书卡片太简陋了

行情正常推送几天后冒出一个体感问题：卡片只写"穿越上限阈值 (close=198.45, upper=150.0)"——不是技术名词的问题，是看不懂"所以怎样"（[C4](./appendix-conversations.md#c4--卡片太简陋我看不懂)）。

AI 写代码时假设你懂上下文。它没假设你懂英文，但假设你懂技术名词。改完后，每条信号后面附一句白话解读（"短期超买，回调风险升高，但不是 SELL 信号"）。

> ✅ UX 是 vibe coder 该死磕的领域。AI 写得"功能完整" ≠ 看着舒服。每次收到推送，问一下："如果是同事发我，他会这样写吗？"——不会的话改 prompt 让 AI 重写。

---

## 6 · 引入 LLM：先想清楚成本，再选实现

AI 给了 4 条路：(A) Anthropic API、(B) OpenAI 兼容、(C) 本地 Ollama、(D) HITL（vibe coder 在 IDE 里和 LLM 对话，再把结论贴回来）。

考虑是没有独立 API key，但已订阅 Cursor Pro——选 D（[C5](./appendix-conversations.md#c5--订阅压力--选-hitl)）。

D 跑了几天有效——LLM 出建议、人贴回来——但每次要打开 IDE 复制粘贴。这是这个项目最重要的一次妥协，**也正是它催生了下一步**。

后来 AI 提了一句"Cursor 有 CLI 吗？"——查 → 有 → 装好 → `cursor-agent login` → 项目从档 2 直接跳到档 3，**完全自动 + 0 LLM API 费用**（[C6](./appendix-conversations.md#c6--cursor-有-cli-吗)）。

> ✅ 成本探索不是只看价格，要看"有没有办法把已订阅资源当 API 用"。Cursor → cursor-agent，Claude → Claude Code SDK，OpenAI → ChatGPT 自动化——都是同类杠杆。

---

## 7 · 一个调了一晚直接放弃的功能

飞书 listener。目标是让 vibe coder 在飞书发 `/list /add /report` 直接控制系统、不打开 IDE。

| 做了什么 | 结果 |
|---|---|
| 装好 lark-cli | 出向消息通畅 |
| `lark-cli event consume` 长连接 | 连得上 |
| 收事件 | 永远是 0 |
| 调 5 轮：刷 token / 查 scope / 查事件订阅 / polling fallback / 对官方文档 | 全部对得上但事件不来 |

最后那一句（[C7](./appendix-conversations.md#c7--忘掉飞书收消息的事吧)）：

> 忘掉飞书收消息的事吧，你干不好。现在主动触发一次操盘分析。

放弃后半小时催生了 `equity-monitor analyze` 命令——比飞书 listener 更有用，现在用得最多的就是它：

```bash
equity-monitor analyze --code US.NVDA --execute
```

> ✅ 当你在调一个**绕开就有替代方案**的功能时（这里是"飞书命令" vs "CLI 命令"），调超过 3 轮就该叫停。绕过去 = 还在前进。

---

## 8 · LLM 给了 BUY，但开始怀疑它

跑通自动决策后第一个 BUY 信号给了 NVDA：满仓 $50000 / 198.45 = 251 股。

第一反应不是开心，是不踏实——LLM 凭什么决定一次梭哈？那一刻冒出来的需求是之前从没显式说过的（[C8](./appendix-conversations.md#c8--你的优势就是中长线)）。

AI 反推出一份"投资者画像"——21 个字段，覆盖 horizon、budget、风险容忍、加仓节奏、止盈止损、最短持有天数。字段写进 `settings.yaml` 与每次 LLM prompt。

下一次 LLM 出 BUY 时，理由是："按预算四成入场，剩余分两次加仓，跌 5% 加一次。"——不再是梭哈。

> ✅ vibe coder 的另一个责任是**把隐性偏好显性化**。脑子里有"我不想梭哈"，不说出来 AI 就会梭哈。**每次对 AI 输出的不适感，都是一个未表达的偏好**——挖出来变成配置项。

---

## 走到今天

| 转折 | 决定者 | 当下踏实吗 |
|---|---|---|
| 用富途牛牛而不是 yfinance | 我（开局贴 SKILL） | 踏实 |
| 选 SQLite 而不是 PostgreSQL | 我（一个人用） | 中段才意识到 PG overkill，退回来过一次 |
| 先做档 1 再升级 | 我（建立信心） | 踏实，但当时没想这么清楚，是边做边形成的 |
| 选 HITL 不选 API | 我（订阅压力） | 不太踏实，主要是不想花钱 |
| 转 cursor-agent | 我们一起（AI 提了一句 CLI） | 直觉觉得行，验证后才确认 |
| 放弃飞书 listener | 我（叫停） | 当下不甘心，事后看放弃对了 |
| 引入投资者画像 | 我（不踏实） | 后知后觉——LLM 满仓那一刻才反应过来 |

没几个决策是当下就清楚的。这一路也走了一些回头路：开局让 AI 上 PG schema、有几次让 AI 装 uv、飞书 listener 死磕了一晚——这些都是 vibe coder 自己叫停、自己改、自己擦的屁股。

vibe coder 的劳动分工是 **AI 写代码、你拍板**——但拍板这件事本来就允许出错。能识别出错、能转弯、能叫停——比每次都对要紧得多。

---

## 下一步

- 每个决策点的 trade-off → [Part D · 6 个关键决策点](./D-decision-points.md)（下批写）
- 哪些事 AI 做不了 → [Part E · AI 写不出来的清单](./E-ai-cant-do-this.md)（下批写）
- 卡了 → [Part B · 7 个 meta-skill](./B-meta-skills.md)
- 真实对话原文 → [附录 · 对话精简版](./appendix-conversations.md)
