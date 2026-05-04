# 中长线投资配置指南

本文档讲清楚 `equity-monitor` 在 **3–6 个月中长线** 视角下都需要哪些配置项、它们各自影响什么、以及三套现成的"配置套餐"——保守 / 平衡 / 进取——直接复制粘贴到 `config/settings.yaml` 即可生效。

---

## 1. 完整配置项一览（`trader.investment_profile`）

> 这一段配置 **跨策略共享**：LLM 策略把它喂进 prompt（让 LLM 用中长线视角思考），rule 策略把它当硬规则护栏（min_holding_days、hard_stop_pct 等）。
>
> 对应代码：`src/equity_monitor/config.py · InvestmentProfileConfig`

### 1.1 总开关

| key | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enabled` | bool | `true` | 关掉 = 退回到旧的纯日内/短线 prompt 模板。默认开。 |

### 1.2 周期 & 风格

| key | 类型 | 默认 | 说明 |
|---|---|---|---|
| `horizon_months_min` | int | `3` | 最短持有期。LLM 看到这个会避免短线信号驱动的买卖。 |
| `horizon_months_max` | int | `6` | 最长持有期。超过这个时长会触发"重评 thesis"。 |
| `style` | enum | `growth` | `growth` / `value` / `blend` / `income` / `speculative` 五选一，影响 LLM 的整体倾向。 |
| `theme` | string | `"AI-infrastructure & cloud-incumbent mid-term swing"` | **自由文本** 的投资 thesis，原样塞给 LLM。≤200 字符；越具体 LLM 越收敛。 |

### 1.3 资金 & 风险

| key | 类型 | 默认 | 说明 |
|---|---|---|---|
| `budget_per_symbol_usd` | float | `50000` | 单个标的目标满仓的美元金额。|
| `drawdown_tolerance_pct` | float | `20` | **可承受**的单标的最大回撤。LLM 据此控制仓位激进度。|
| `max_concentration_pct` | float | `60` | 单标的占组合的硬上限。即便 `budget_per_symbol_usd` 还能加仓，触此线即拒。|
| `cash_reserve_pct` | float | `10` | 永远不投的现金缓冲。今天仅信息性；M-3 组合层 sizing 会强制执行。|

### 1.4 入场 & 加仓策略

| key | 类型 | 默认 | 说明 |
|---|---|---|---|
| `initial_entry_pct` | float | `40` | 第一次买入用 `budget` 的多少 %。设为 100 即一把梭。|
| `max_batches` | int | `3` | 最多分几批加仓。超过后 BUY 一律拒。|
| `add_on_dip_pct` | float | `5` | 加仓门槛——必须**比上次买入价至少跌 N%**才允许追加（防摊低成本陷阱）。|
| `add_cooldown_days` | int | `5` | 两次加仓之间最短间隔（防恐慌摊薄）。|
| `prefer_dip_buy` | bool | `true` | 提示 LLM 偏好回调入场（RSI<40、贴布林下轨、MACD 负但翘头）。|
| `earnings_blackout_days` | int | `3` | 财报前 N 天不开新仓。**Reserved**——需要财报日历数据源（M-3）。|

### 1.5 退出策略

| key | 类型 | 默认 | 说明 |
|---|---|---|---|
| `take_profit_pct` | float | `30` | 浮盈触发部分止盈的阈值。设为 0 关闭。|
| `take_profit_trim_pct` | float | `50` | 触发后卖出仓位比例。100 = 全部止盈出场。|
| `hard_stop_pct` | float | `20` | 浮亏达此 % 强制 SELL（**忽略 LLM 意见**）。建议跟 `drawdown_tolerance_pct` 一致或更紧。|
| `trailing_stop_pct` | float \| null | `null` | 移动止损（从持有期最高点回撤 N% 卖出）。**Reserved**（M-3 需要 high-watermark 跟踪）。|
| `min_holding_days` | int | `30` | 持有不到 N 天禁止主动 SELL（`hard_stop_pct` 触发能绕过）。防 LLM 噪声扰动。|

### 1.6 LLM Prompt 辅助

| key | 类型 | 默认 | 说明 |
|---|---|---|---|
| `news_lookback_days` | int | `7` | 新闻回看窗口。**Reserved**——C2b 新闻管线接入后启用。|
| `rebalance_cadence_days` | int | `30` | 每隔 N 天用 long-form review prompt 重评 thesis。**Reserved**。|
| `valuation_ceiling_pe` | float \| null | `null` | 远期 PE 超过这个时 LLM 应拒 BUY。**Reserved**——需要基本面数据源。|

---

## 2. 推荐套餐（直接复制粘贴）

> 把下面任意一段贴到 `config/settings.yaml` 的 `trader.investment_profile:` 节点下替换即可。
>
> **怎么选**：先想清楚两个问题——
> 1. 你这笔钱**能不能容忍 -20% 的暂时浮亏**？不能 → 选保守；能 → 平衡或进取。
> 2. 你**主动盯盘**还是**佛系**？盯盘多 → 进取（短 cooldown）；佛系 → 平衡或保守（长 cooldown）。

### 🛡 套餐 A · 保守（Conservative）

适合：**不想被回撤吓到、希望稳健建仓、首仓不过半**。

```yaml
investment_profile:
  enabled: true

  horizon_months_min: 6
  horizon_months_max: 12
  style: blend
  theme: "Quality mega-cap accumulation, prefer technical pullbacks, exit on macro regime shift"

  budget_per_symbol_usd: 50000
  drawdown_tolerance_pct: 12
  max_concentration_pct: 40
  cash_reserve_pct: 20

  initial_entry_pct: 25       # 一把只先买 1/4 仓
  max_batches: 4
  add_on_dip_pct: 8           # 至少 -8% 才加仓
  add_cooldown_days: 10
  prefer_dip_buy: true
  earnings_blackout_days: 5

  take_profit_pct: 20         # 早一点开始止盈
  take_profit_trim_pct: 33
  hard_stop_pct: 12
  trailing_stop_pct: null
  min_holding_days: 45        # 持有 ≥ 1.5 个月再考虑卖

  news_lookback_days: 14
  rebalance_cadence_days: 30
  valuation_ceiling_pe: 35
```

**特点**：首仓小、加仓门槛高、止损紧、持有期长。LLM 会更倾向 HOLD，BUY 信号要更强的回调和确定性。

### ⚖️ 套餐 B · 平衡（Balanced，**默认推荐**）

适合：**可接受 -20% 浮亏、希望分批建仓、追求中线趋势**。**这就是 `settings.yaml` 当前默认。**

```yaml
investment_profile:
  enabled: true

  horizon_months_min: 3
  horizon_months_max: 6
  style: growth
  theme: "AI-infrastructure & cloud-incumbent mid-term swing"

  budget_per_symbol_usd: 50000
  drawdown_tolerance_pct: 20
  max_concentration_pct: 60
  cash_reserve_pct: 10

  initial_entry_pct: 40       # 首仓 4 成
  max_batches: 3
  add_on_dip_pct: 5
  add_cooldown_days: 5
  prefer_dip_buy: true
  earnings_blackout_days: 3

  take_profit_pct: 30
  take_profit_trim_pct: 50
  hard_stop_pct: 20
  trailing_stop_pct: null
  min_holding_days: 30

  news_lookback_days: 7
  rebalance_cadence_days: 30
  valuation_ceiling_pe: null
```

**特点**：3-6 月中线、4 成首仓、3 次分批、+30% 半止盈、-20% 硬止损。覆盖大多数科技中长线场景。

### 🚀 套餐 C · 进取（Aggressive）

适合：**风险偏好高、相信短期催化、能接受 -30% 浮亏、主动盯盘**。

```yaml
investment_profile:
  enabled: true

  horizon_months_min: 2
  horizon_months_max: 6
  style: growth
  theme: "High-conviction AI / semis swing trades, ride momentum, exit decisively on thesis break"

  budget_per_symbol_usd: 50000
  drawdown_tolerance_pct: 30
  max_concentration_pct: 80
  cash_reserve_pct: 5

  initial_entry_pct: 60       # 首仓 6 成
  max_batches: 2
  add_on_dip_pct: 3           # -3% 就加仓
  add_cooldown_days: 2
  prefer_dip_buy: false       # 也接受突破入场（金叉 / 上轨突破）
  earnings_blackout_days: 0

  take_profit_pct: 50         # 浮盈 +50% 才止盈
  take_profit_trim_pct: 33
  hard_stop_pct: 25
  trailing_stop_pct: 15       # 启用移动止损（M-3 启用前是 informational）
  min_holding_days: 14

  news_lookback_days: 5
  rebalance_cadence_days: 14
  valuation_ceiling_pe: null
```

**特点**：首仓重、加仓激进、止盈晚、可接受突破入场。LLM 更愿意触发 BUY，但搭配 `hard_stop_pct: 25` 控制单标的下行。

### 套餐快速对照

| 维度 | 保守 | 平衡 | 进取 |
|---|---|---|---|
| 持有期 | 6–12 月 | 3–6 月 | 2–6 月 |
| 单标的可承受回撤 | 12% | 20% | 30% |
| 首仓比例 | 25% | 40% | 60% |
| 最多分批 | 4 | 3 | 2 |
| 加仓回调门槛 | -8% | -5% | -3% |
| 加仓冷却 | 10 天 | 5 天 | 2 天 |
| 止盈触发 | +20% | +30% | +50% |
| 止盈卖出比例 | 33% | 50% | 33% |
| 硬止损 | -12% | -20% | -25% |
| 最短持有 | 45 天 | 30 天 | 14 天 |
| LLM 倾向 | HOLD-bias | 平衡 | BUY-bias |

---

## 3. 使用指南

### 3.1 改完配置怎么生效？

`investment_profile` 在每次 LLM 调用时**实时读取** `settings.yaml`，无需重启 scheduler——但有个**例外**：`run` 命令启动后已 cached 配置，要让定时任务感知新配置仍需重启。

```bash
# 主动触发分析（立刻吃到新配置）
equity-monitor analyze

# 定时任务则需要重启
pkill -f "equity-monitor run"
nohup equity-monitor run > var/scheduler.log 2>&1 &
```

### 3.2 主动触发分析：`equity-monitor analyze`

```bash
# 默认：跑全 watchlist，吃当前 settings.yaml 的 profile
equity-monitor analyze

# 限定标的
equity-monitor analyze --code US.NVDA

# 临时 override（不改 settings.yaml）
equity-monitor analyze --code US.NVDA --budget 30000 --drawdown 15

# 主动触发 + 自动下单到富途 OpenD SIMULATE
equity-monitor analyze --execute

# JSON 输出（接管道用）
equity-monitor analyze --json | jq '.[] | select(.decision.action == "BUY")'
```

`--execute` 会把每个非 HOLD 决策落到 `signals` + `trades` + `positions` 表，并提交订单到富途模拟账户——你可以在**富途牛牛 App / 网页 / 桌面端**的"模拟账户"里直接看到。

### 3.3 定时任务的行为差异

| 触发方式 | 何时叫 LLM | 是否吃 profile |
|---|---|---|
| `equity-monitor run`（cron 定时） | **仅**触发了 signal（价格穿阈值/技术指标异动）时 | ✅ |
| `equity-monitor once --job intraday` | 同上 | ✅ |
| `equity-monitor analyze`（手动） | **每次都叫**，无论有没有 signal | ✅ |

设计上 cron 路径**只在有 signal 时**调 LLM，是为了节约 token。如果你想"定时不论是否有 signal 都跑一次中长线 review"，目前需要用 cron 调用 `analyze`：

```cron
# crontab 示例：北京时间每天 22:00 主动 review 一次
0 22 * * 1-5 cd /path/to/equity-monitor && /path/to/equity-monitor analyze >> var/analyze.log 2>&1
```

### 3.4 常见问题

**Q: LLM 输出和我的 profile 矛盾怎么办？**
比如 profile 是保守套餐 +12% drawdown，但 LLM 给 BUY 251 股全仓——`enforce_constraints` 会先按 `min_confidence` 把它降级，超 `max_position` 也会拒。但 `hard_stop_pct` 这种"账户层硬规则"今天**还没强制执行**（M-3 才接管）；目前 LLM 只是被告知这些，**自由发挥**程度依然较高。如果发现 LLM 总越界，把 `style` 调成 `value` 或 `blend`、`prefer_dip_buy: true`、收紧 `max_concentration_pct` 通常就能扳回来。

**Q: 推荐用哪个 LLM provider？**
当前默认 `cursor-agent`（吃你的 Cursor 订阅，零 API key）。Anthropic / OpenAI / DeepSeek / Doubao 都能接，配 `provider` + `api_key_env` 即可（详见 `config/settings.yaml` 注释）。

**Q: 套餐 A/B/C 之外想自己调？**
就改 `settings.yaml` 对应字段就行，不会破坏任何现有功能。每改一项最好先 `equity-monitor analyze --code US.NVDA --json` 看一眼 LLM 是否如预期反应再 rollout。

**Q: 历史交易和盈亏在哪看？**
- 飞书 App → 切「模拟」账户 → 持仓/委托
- 项目内：`equity-monitor trade list --status executed`
- DB：`sqlite3 data/equity_monitor.db "SELECT * FROM trades; SELECT * FROM positions;"`

---

## 4. Reserved / 未来工作

下面这些字段已经在 schema 里、但运行时**还没强制执行**，文档单独标 `Reserved`——用户可以提前填，等代码补齐自动生效：

- `cash_reserve_pct` — 等组合层 sizing（M-3）
- `earnings_blackout_days` — 等财报日历数据源
- `trailing_stop_pct` — 等持有期 high-watermark 跟踪（M-3）
- `take_profit_pct` / `take_profit_trim_pct` / `hard_stop_pct` 的**自动触发** — 当前 LLM 知道这些数字、会主动卖；但"账户层 cron 守门员"还没建（M-3 portfolio supervisor）
- `valuation_ceiling_pe` — 等基本面数据源
- `rebalance_cadence_days` — 等 long-form review prompt（M-2 后期）
- `news_lookback_days` — 等 C2b 新闻 ingestion 上线

填了不会报错，时机到了自动生效。
