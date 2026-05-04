# Part E · "AI 写不出来"的清单

vibe coder 的痛点不是"代码写不出来"——AI 能写。痛点是**有些事 AI 替不了**，必须自己干。这里列 6 类。

---

## 1 · 投资 thesis

NVDA 还是 MSFT、5 万还是 50 万、3 个月还是 3 年、20% 回撤还是 50% 回撤——AI 没立场替你定。

让 AI 帮你"挑标的"看似省事，实际拿到的是公开知识的均值——它会给你 AAPL/MSFT/NVDA 这种几乎人人都在配的组合。这种组合**不是错的**，但也不是"你的"——你说不清为什么持有，跌了就拿不住。

让 AI 帮你定 budget 看似科学，实际是把"你能承受多少损失而不影响生活"这种心理状况算进 ROI 公式——AI 既不知道你存款多少、也不知道你工作稳不稳。

**vibe coder 必须自己做的**：定标的、定金额、定持有周期、定可承受亏损。这 4 个数定下来才有"项目宪法"（[A 第 1 节](./A-kickoff.md#项目宪法是边走边长出来的)）。

**AI 能帮你做的**：把这 4 个数翻译成 prompt 字段、配置项、风控规则。

---

## 2 · 平台 OAuth / 后台配置

AI 看不到你打开的网页。

这个项目里 AI 假设性出错过的几个地方：

- **飞书机器人 scope** —— AI 反复说"应该是 scope 没开"，让 vibe coder 改飞书后台 → 改完还是不行（[B 第 5 节](./B-meta-skills.md#5--ai-想猜原因时让它先看原始材料)）；最后是 vibe coder 跑了 `lark-cli auth scopes --format json` 才发现 AI 一直在拿 user 视角的 scope 表去判断 bot 视角应有的 scope
- **富途 OpenD 登录** —— AI 不知道 OpenD 装了没、模拟交易开了没、当前默认账户是哪个；都要 vibe coder 手动确认
- **cursor-agent OAuth** —— `cursor-agent login` 弹的是浏览器，AI 看不到回调结果；只能 vibe coder 看到 `✓ Logged in as ...` 后告诉它

**vibe coder 必须自己做的**：所有跨进程 OAuth、后台配置、扫码登录——动作本身和**把结果告诉 AI**。

**给 AI 的姿势**：每次它说"应该是 X"——问一句"根据什么"。答不上来就让它列具体命令、要 vibe coder 执行后贴回 stdout。**别让它在猜的时候改你的飞书/富途/Cursor 后台**。

---

## 3 · 订阅决策 / 资源边界

AI 不知道你订阅过什么、月预算多少、公司能报销什么。

这个项目最大的成本杠杆——cursor-agent CLI——之所以能用，是因为 vibe coder 已经付了 Cursor Pro 月费（[D 第 4 节](./D-decision-points.md#4--llm-选型hitl--cursor-agent-这条转弯)）。如果开局让 AI 选 LLM 路线，它会默认推 Anthropic API 或 OpenAI——技术上没错，但月成本可能多 $50–200。

类似的事：

- 富途账号（已开） → 富途 API 默认免费送
- 飞书企业账号（公司在用） → lark-cli 不用单独申请
- VPN（没买） → AI 提的"用 OpenAI"会卡在网络

**vibe coder 必须自己做的**：把"我已经有什么"显式列出来贴给 AI（[A 资源选型](./A-kickoff.md#资源选型是-vibe-coder-真正动脑子的地方)），AI 才能在这堆里挑实现。

**反姿势**：让 AI 推荐"最佳栈"——它不知道你的预算和已订阅，给的建议跟你的资源池零交集。

---

## 4 · 本机环境装机

AI 假设标准环境，**本机几乎从不是标准环境**。

这个项目里"AI 跑通你跑不通"的真实根源都在这：

- conda env 名叫 `fin`，AI 默认用 `base`
- cwd 在 `~/Documents/Code/equity-monitor`，AI 假设是项目根
- OpenD 端口 11111，AI 默认 22222
- lark-cli 的 `--file` 参数只接受相对路径（[B 第 6 节](./B-meta-skills.md#6--报错把-traceback-完整贴回去)）

每次踩这种坑都是 vibe coder 自己亲自跑命令、亲自看错误、亲自把完整 traceback 贴回去——**AI 不可能远程感知你的本机**。

**vibe coder 必须自己做的**：

1. 装机：OpenD、conda env、Python 包、cursor-agent、lark-cli
2. 把环境约束**显式写进 system prompt**：env 名字、cwd、端口、平台（macOS / Linux / Apple Silicon）
3. 报错时贴完整 stderr + traceback，别让 AI 凭印象修

---

## 5 · 审美和"不踏实"感

AI 不知道**你看着舒不舒服**。

这个项目至少 3 次重写都是被"不踏实"驱动的：

| 不踏实 | 触发的改动 |
|---|---|
| 飞书卡片只写"穿越上限阈值 (close=198.45)"——看不懂"所以怎样" | 加 `interpret.py`，给每个信号附白话解读（[C4](./appendix-conversations.md#c4--卡片太简陋我看不懂)）|
| LLM 第一次给 BUY 直接梭哈 251 股——凭什么 | 引入投资者画像 21 个字段（[C8](./appendix-conversations.md#c8--你的优势就是中长线)）|
| `has_suggestion=True` 但 LLM 决策是 HOLD——日志看着别扭 | 改成 `suggestion=<action> qty=<n>` 显式记录 |

每次"不踏实"都是 vibe coder 脑子里**多出来一条隐性偏好**——AI 不会主动问、不会替你想到。

**vibe coder 必须自己做的**：

- 真用产品。每次推送一打开就觉得别扭——记下来去改
- 把不踏实**说出来**。"这看着别扭"AI 听不懂，要翻译成"我希望显示 X 而不是 Y"
- 把不踏实**变成配置或 prompt 字段**，下次一开始就避开

**反姿势**：把不踏实憋着不说——AI 写代码会越走越远，最后大改成本翻倍。

---

## 6 · 决定何时收尾

AI 永远倾向"再加一层"。

这个项目能跑后，AI 主动建议过的"还能再做"清单大致是：

- 接 Bloomberg / Polygon 数据做基本面
- 加机构持仓 / 期权 flow 信号
- 做回测引擎对策略历史表现做评估
- 接 vectorbt 做向量化回测
- 加多策略集成 + ensemble voting
- 加移动 App 推送（替代飞书）
- 接公司大模型做 fine-tuning

每条都"听起来有道理"。**全部做完，项目就死在工程债里了**。

**vibe coder 必须自己做的**：识别"够用线"。这个项目的够用线是——**LLM 给我有理有据的 BUY/SELL，自动下到富途模拟账户，飞书一推送我能看懂**。线划在这之后的所有功能，都要明确判断："不做行不行？"

行就不做。

**给 AI 的姿势**：当 AI 提议新功能时，问一句——"**这个不做的话，最坏情况是什么？**" 答不上"最坏情况"就不做。

---

## 速查

| 类别 | 谁干 | 给 AI 的姿势 |
|---|---|---|
| 投资 thesis | 你定 | 把数字告诉 AI |
| OAuth / 后台 | 你操作 | 把结果贴回去；别让 AI 在猜的时候改后台 |
| 订阅 / 资源 | 你列清单 | 一次性贴；让 AI 在这堆里挑 |
| 本机装机 | 你装 + 写进 system prompt | 报错贴完整 traceback |
| 审美 / 不踏实 | 你识别 + 翻译 | "我希望显示 X 而不是 Y" |
| 何时收尾 | 你拍板 | "不做行不行？最坏情况是什么？" |

---

## 下一步

- 项目怎么一步步长出来的 → [Part C · 项目是怎么长出来的](./C-evolution-timeline.md)
- 关键决策的展开 → [Part D · 6 个关键决策点](./D-decision-points.md)
- 直接动手 → [Part F · 最小骨架](./F-skeleton.md)
