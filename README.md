# Equity Monitor — 用户手册

一个驻留在本地的美股盯盘 + 模拟自动交易系统。每个 NYSE 交易小时自动取价 → 算指标 → 出信号 → 推飞书卡片 → 默认直接走 Futu SIMULATE 账户下单。同时支持 K 线快照、飞书双向命令、历史回看和 P&L。

> **架构定位**：长跑后台（`equity-monitor run`）+ 飞书消息听器（`equity-monitor listen`）+ 一组手动 CLI。状态全部落 SQLite，跨重启幂等。

---

## 目录

1. [快速上手（5 步）](#快速上手5-步)
2. [它能做什么](#它能做什么)
3. [系统工作原理](#系统工作原理)
4. [日常运行 & 操作](#日常运行--操作)
5. [飞书命令参考](#飞书命令参考)
6. [CLI 命令参考](#cli-命令参考)
7. [配置文件](#配置文件)
8. [数据模型](#数据模型)
9. [故障排查](#故障排查)
10. [测试 / 开发](#测试--开发)
11. [已知限制 & Roadmap](#已知限制--roadmap)

---

## 快速上手（5 步）

```bash
# 1) OpenD（富途本地 API 服务，一次性装）
bash scripts/install_opend.sh
python scripts/check_opend.py    # 验证 127.0.0.1:11111 可达
# → 用富途账号登录 OpenD，并把账号设为「模拟账户」(SIMULATE)

# 2) Python 环境
conda create -n fin python=3.11 -y
conda activate fin
pip install -e ".[dev]"

# 3) 配置
cp config/watchlist.example.yaml config/watchlist.yaml   # 编辑你的标的 + 上下阈值
# 编辑 config/settings.yaml → lark.receiver.open_id 改成你的飞书 open_id：
lark-cli contact +get-user | jq -r '.data.user.open_id'

# 4) 初始化 DB + 同步 watchlist
equity-monitor db init
equity-monitor watchlist sync
equity-monitor backfill --days 30        # 拉 30 天历史 K 线 + 指标（推荐）

# 5) 跑起来（tmux 双窗格）
tmux new -s equity
equity-monitor run                       # 窗格 1：调度器
# Ctrl-B " 切窗格 2：
equity-monitor listen                    # 飞书消息听器
# Ctrl-B D 离开（继续后台跑）；tmux kill-session -t equity 完全关停
```

跑起来之后你会在飞书 DM 收到：

- 美东每个整点 30 分的盯盘卡（含 K 线快照 PNG + 指标解读 + 自动成交回执）
- 上午 10:30 早报 / 下午 16:30 收盘小结
- 每 30 分钟一次的新闻情绪脉搏

---

## 它能做什么


| 场景                | 怎么用                                                                                                  |
| ----------------- | ---------------------------------------------------------------------------------------------------- |
| **盯盘 + 自动告警**     | `equity-monitor run` 后台跑；价格 / RSI / MACD / Bollinger / 阈值突破都会触发飞书卡片                                  |
| **自动模拟交易**        | 默认开。每次盯盘 cron 命中 BUY/SELL 建议时，自动通过 Futu SIMULATE 账户下单并写入 `trades` 表                                  |
| **K 线快照**         | 飞书 `/chart US.AAPL D` 或终端 `equity-monitor chart US.NVDA --freq 60m --push`；带买卖点和成本均价线                |
| **改 watchlist**   | 在飞书直接 DM `添加 US.AAPL 上限200 下限165`、`阈值 NVDA 上限150`、`删除 TSLA`、`列表`                                     |
| **看历史 / 持仓 / 盈亏** | `equity-monitor trade positions` / `equity-monitor trade pnl --days 7` / `equity-monitor trade list` |
| **手动确认 / 取消建议**   | `auto_execute=false` 时使用 `equity-monitor trade confirm <signal_id>` / `cancel <signal_id>`           |
| **回填 / 重置**       | `equity-monitor backfill --days N` / `equity-monitor db status` 看各表行数                                |


### 信号种类（出现在告警卡里）


| 信号                                  | 严重度           | 来源                      |
| ----------------------------------- | ------------- | ----------------------- |
| `threshold_breach_upper / lower`    | CRITICAL      | watchlist 里的上下阈值        |
| `rsi_overbought / oversold`         | WARN          | RSI(14) 穿越 70 / 30      |
| `macd_golden_cross` / `death_cross` | INFO / WARN   | MACD 线上穿 / 下穿信号线        |
| `boll_upper_break / lower_break`    | INFO          | 收盘价穿越布林带                |
| `futu_tech_anomaly`                 | INFO/CRITICAL | 富途技术异动；反转形态升级到 CRITICAL |
| `futu_capital_anomaly`              | WARN          | 富途大单异动                  |
| `news_pulse_pos / neg`              | WARN          | 评论情绪温度变化 ≥ 3.0          |


`signals.dedupe_window_minutes`（默认 60 分钟）窗内的同号信号会被去重。

### 自动交易策略（hard-coded `strategy_lite`）

由 `signals/strategy_lite.py` 5 条规则确定，**无 LLM、无 ML、纯规则**：


| 优先级 | 触发信号                                    | 决策                     |
| --- | --------------------------------------- | ---------------------- |
| 1   | `threshold_breach_lower`                | BUY 100 股              |
| 2   | `threshold_breach_upper`                | SELL 当前持仓全部（无仓位则 HOLD） |
| 3   | `rsi_oversold` AND `macd_golden_cross`  | BUY 50 股               |
| 4   | `rsi_overbought` AND `macd_death_cross` | SELL min(持仓, 50)       |
| 5   | `boll_lower_break`                      | BUY 50 股               |
| -   | 其他组合                                    | HOLD                   |


数量、阈值都写在 `signals/strategy_lite.py` 顶部的常量里，要改去那里改。

### 安全护栏

- **SIMULATE-only**：`OpenDSecTrader` 启动时主动找 SIMULATE 账户；找不到直接 raise，绝不碰真实钱包
- **错误隔离**：单标的下单拒绝不影响其他标的；OpenDSecTrader 初始化失败只关掉这一轮自动交易，盯盘告警继续推
- **幂等**：同一 `(symbol, ts, signal_type)` 只下一次单；调度器重启 / cron 重跑都不会双开
- **PENDING 单不污染持仓**：闭市后下的 SIMULATE 单状态是 PENDING，会写 `trades` 但**不**更新 `positions`（避免 qty=100 / avg_cost=0 这种脏数据）

---

## 系统工作原理

```
                                              ┌────────────────────────┐
       APScheduler                             │    Futu OpenD :11111   │
       (NYSE calendar)                         │   行情 + SIMULATE 交易  │
            │                                  └─────────┬──────────────┘
            ▼                                            │
  每整点 30 分           ┌──────────────────────────────┐ │
  intraday_check  ────► │ run_intraday_check           │◄┘
                        │  ① 取 snapshot + K 线         │
                        │  ② 算 RSI/MACD/BOLL          │
                        │  ③ 阈值 + 技术信号 → Signal   │
                        │  ④ strategy_lite → Suggest   │
                        │  ⑤ 落 SignalRow              │
                        │  ⑥ 自动执行 BUY/SELL ────────┐│
                        │  ⑦ 渲染卡片 + K 线快照 PNG    ││
                        └─────┬────────────────────────┘│
                              │  飞书卡片 + PNG          │
                              ▼                          │
                        ┌──────────┐                     │
                        │ lark-cli │                     │
                        └────┬─────┘                     │
                             │                            ▼
                             ▼                 ┌────────────────────┐
                        飞书 DM / 群           │ trader/execute.py  │
                                              │ Trade + Position   │
                              ▲                │ → SQLite           │
                              │                └────────────────────┘
                        ┌──────────┐
                        │ listener │ ◄── 飞书 WS 事件 (im.message.receive_v1)
                        │  /add    │     /remove /list /threshold /chart …
                        └──────────┘
```

3 个长跑组件：

- `**equity-monitor run**` — APScheduler，按 cron 触发 4 个 job
- `**equity-monitor listen**` — 飞书消息听器，处理双向命令
- **OpenD**（富途）— 必须先开起来，提供行情 + SIMULATE 交易

### 每天的 4 个定时任务


| Job              | Cron (America/New_York) | 用途                                                  |
| ---------------- | ----------------------- | --------------------------------------------------- |
| `intraday_check` | `30 9-15 * * mon-fri`   | 每整点 30 分：snapshot + 指标 + 信号 + **自动交易** + 卡片 + K 线快照 |
| `morning_brief`  | `30 10 * * mon-fri`     | 开盘 1 小时后：涨跌幅榜                                       |
| `closing_brief`  | `30 16 * * mon-fri`     | 收盘后：当日总结                                            |
| `news_pulse`     | `*/30 9-15 * * mon-fri` | 每 30 分钟：新闻 + 情绪温度突变检测                               |


NYSE 节假日（含早闭）和 DST 由 `pandas-market-calendars` 自动处理，非交易日整体跳过。

---

## 日常运行 & 操作

### 启动 / 停止

```bash
# 启动（推荐 tmux）
tmux new -s equity
conda activate fin
equity-monitor run                # 窗格 1：调度器
# Ctrl-B "
equity-monitor listen             # 窗格 2：飞书听器
# Ctrl-B D 离开

# 停止
tmux kill-session -t equity       # 完全关停
# 或者 SIGINT / SIGTERM 单个进程
```

### 临时手跑一轮（不影响调度器）

```bash
equity-monitor once --job intraday              # 用 cfg.trader.auto_execute 决定是否下单
equity-monitor once --job intraday --no-auto-trade   # 强制不下单（debug / dry-run）
equity-monitor once --job intraday --auto-trade      # 强制下单（即使 settings.yaml 是 false）
equity-monitor once --job morning
equity-monitor once --job closing
equity-monitor once --job news
```

返回值是个 dict，例如：

```
{'quotes': 3, 'signals': 4, 'pushed': 3, 'suggestions': 1, 'executed': 1}
```

字段含义：

- `quotes` 新写入的 quotes 行数
- `signals` 本轮产生的去重后信号数
- `pushed` 推送到飞书的卡片数
- `suggestions` 非 HOLD 的策略建议数
- `executed` 真正下单成功的数（auto_execute=false 或 paper_trader=None 时为 0）

### 看当前状态

```bash
equity-monitor db status                  # 各表行数
equity-monitor watchlist list             # DB 里激活的标的
equity-monitor trade positions            # 当前持仓 + 未实现 P&L（DB 端）
equity-monitor trade pnl --days 7         # 最近 7 天已实现 P&L
equity-monitor trade list --status pending     # 待确认建议（auto_execute=false 时用）
equity-monitor trade list --status executed    # 已成交
```

### 手动场景：临时关掉自动交易

两种姿势：

```bash
# 方式 A：永久关。改 settings.yaml → trader.auto_execute: false → 重启 run。
#         之后所有信号只出建议，等你 trade confirm 才下单。

# 方式 B：单次关。
equity-monitor once --job intraday --no-auto-trade
```

`auto_execute=false` 时的人工流程：

```bash
equity-monitor trade list --status pending          # 看建议列表
equity-monitor trade confirm 7                      # 确认下单（可加 --qty 50 覆盖建议数量）
equity-monitor trade cancel 7                       # 直接取消
```

### 改 watchlist（不重启）

在飞书 DM 给 bot：

```
添加 US.AAPL 上限200 下限165
阈值 US.NVDA 上限150 下限110
删除 TSLA
列表
```

或者改 `config/watchlist.yaml` + `equity-monitor watchlist sync`。

---

## 飞书命令参考

`equity-monitor listen` 必须在跑。命令支持斜杠、中文、自然语言三种风格，**仅 `lark.receiver.open_id` 配置的本人**能改 watchlist（其他发件人会被忽略）。


| 操作    | 写法示例                                                                      |
| ----- | ------------------------------------------------------------------------- |
| 添加    | `添加 US.AAPL 上限200 下限165` ／ `/add US.AAPL upper=200 lower=165` ／ `监控 TSLA` |
| 删除    | `删除 US.AAPL` ／ `取消 AAPL` ／ `/remove US.AAPL`                              |
| 改阈值   | `阈值 US.AAPL 上限205` ／ `/threshold US.AAPL upper=205 lower=170`             |
| 列表    | `列表` ／ `/list`                                                            |
| K 线快照 | `/chart US.AAPL` ／ `/chart AAPL D` ／ `图 TSLA` ／ `chart NVDA 15m`          |
| 帮助    | `帮助` ／ `/help`                                                            |


`/chart` 支持频率：`5m`、`15m`、`30m`、`60m`（默认）、`D`、`W`。`1m` 噪声太大未开放。

### 听器后端

```bash
equity-monitor listen                                         # 默认 websocket（推荐）
equity-monitor listen --backend polling --poll-interval 10   # 回退轮询
equity-monitor listen --rich-cards                           # 默认开（含实时价格 + 指标解读卡片）
equity-monitor listen --text-only                            # 关闭，回纯 markdown
```

> WebSocket 后端要求飞书后台已注册 `im.message.receive_v1` 事件 + 当前 `lark-cli` 进程是该 bot 的唯一订阅者（多个会被服务端轮询切走）。

---

## CLI 命令参考

```
equity-monitor [--settings PATH] [--watchlist PATH]
├── run                                启动长跑调度器（阻塞，SIGINT/TERM 停止）
├── listen                             启动飞书消息听器（阻塞）
│     [--backend websocket|polling] [--poll-interval N] [--rich-cards/--text-only]
├── once --job intraday|morning|closing|news
│                                      手动跑一次某个 job 并打印结果 dict
│     [--auto-trade|--no-auto-trade]   覆盖 cfg.trader.auto_execute（仅当本次 intraday）
├── backfill [--days N]                回填 60-min OHLC + 指标（默认 30 天，幂等）
│
├── chart TICKER                       渲染 K 线快照 PNG（可选推飞书）
│     [--freq 60m|5m|15m|30m|D|W]      (default 60m)
│     [--out-dir PATH]                 (default var/snapshots)
│     [--push|--no-push]                (default --no-push)
│
├── watchlist
│   ├── list                           列出 DB 中激活的标的
│   └── sync                           把 config/watchlist.yaml upsert 到 symbols 表
│
├── trade
│   ├── list [--status pending|confirmed|executed|cancelled|all]
│   │                                  看交易建议（默认 pending）
│   ├── confirm SIGNAL_ID [--qty N]    手动下单（auto_execute=false 时用）
│   ├── cancel SIGNAL_ID               把 pending 建议标记为 cancelled
│   ├── positions                      持仓 + 未实现 P&L（DB 端 mark-to-market）
│   └── pnl [--days N]                 已实现 P&L 按标的聚合（默认 7 天）
│
└── db
    ├── init                           创建 SQLite schema
    └── status                         打印各表行数
```

### CLI 用法示例

```bash
# 1. 渲染 AAPL 60 分 K 线，保存本地 + 推飞书
equity-monitor chart US.AAPL --freq 60m --push

# 2. 看上周已实现盈亏
equity-monitor trade pnl --days 7

# 3. 手动确认 signal 12 的建议，按建议数量下单
equity-monitor trade confirm 12

# 4. 手动确认 signal 12 但只下 30 股
equity-monitor trade confirm 12 --qty 30

# 5. 临时强制干跑（不下单），用来 debug 当前指标 / 信号是否正常
equity-monitor once --job intraday --no-auto-trade
```

`--settings` / `--watchlist` 默认是 `config/{settings,watchlist}.yaml`（相对当前工作目录）。配置是惰性加载的，所以 `--help` 在配置不存在时也能跑。**所有命令都默认从 repo 根目录跑**；要在别处跑就 `equity-monitor --settings /abs/path/to/settings.yaml ...`。

---

## 配置文件

### `config/settings.yaml`

```yaml
opend:
  host: 127.0.0.1
  port: 11111

database:
  path: data/equity_monitor.db
  wal_mode: true                    # SQLite WAL；多进程读写更友好

scheduler:
  timezone: America/New_York
  jobs:
    intraday_check: { cron: "30 9-15 * * mon-fri" }
    morning_brief:  { cron: "30 10 * * mon-fri" }
    closing_brief:  { cron: "30 16 * * mon-fri" }
    news_pulse:     { cron: "*/30 9-15 * * mon-fri" }

lark:
  cli_path: lark-cli
  identity: bot                     # bot 无需额外 scope；user 需 `lark-cli auth login --scope im:message.send_as_user`
  receiver:
    type: user                      # user → DM 发给 open_id；chat → 群发
    open_id: "ou_xxx..."

signals:
  rsi_overbought: 70
  rsi_oversold: 30
  bollinger_period: 20
  bollinger_std: 2
  macd_fast: 12
  macd_slow: 26
  macd_signal: 9
  dedupe_window_minutes: 60         # 同信号去重窗口
  news_burst_drop: 3.0              # 情绪温度突变阈值
  news_burst_rise: 3.0

logging:
  level: INFO
  file: data/equity_monitor.log

trader:
  auto_execute: true                # ★ 是否自动下模拟单（默认 ON）
  simulate_only: true               # 永远 true；防呆，禁止接真实账户
```

### `config/watchlist.yaml`

```yaml
symbols:
  - code: US.AAPL                   # 必须 US./HK./SH./SZ. 前缀
    name: Apple
    upper_threshold: 200.0          # 收盘 > upper → CRITICAL → SELL all
    lower_threshold: 165.0          # 收盘 < lower → CRITICAL → BUY 100
    notes: "core position"
  - code: US.NVDA
    name: NVIDIA
    upper_threshold: 150.0
    lower_threshold: 110.0
  - code: US.TSLA                   # 不写阈值就只监控 RSI/MACD/BOLL/异动信号
    name: Tesla
```

改完后跑 `equity-monitor watchlist sync`（或在飞书直接 `/add`）让它生效。

---

## 数据模型

SQLite，8 张表，全部由 `equity-monitor db init` 创建：


| 表                     | 主键              | 关键字段                                                                                                           | 用途                  |
| --------------------- | --------------- | -------------------------------------------------------------------------------------------------------------- | ------------------- |
| `symbols`             | id              | code, name, upper_threshold, lower_threshold, is_active                                                        | watchlist DB 镜像     |
| `quotes`              | id              | symbol_id, ts, last_price, open/high/low, volume, turnover                                                     | 实时 snapshot 历史      |
| `indicators`          | (symbol_id, ts) | rsi_14, macd, macd_signal, macd_hist, boll_*                                                                   | 每根 K 线的指标值          |
| `signals`             | id              | symbol_id, ts, signal_type, severity, payload_json, suggested_action, suggested_qty, status, executed_trade_id | 每个信号 + 关联的策略建议 + 状态 |
| `trades`              | id              | symbol_id, ts, side, qty, price, futu_order_id, signal_id, status (FILLED/PENDING/REJECTED)                    | 模拟交易历史              |
| `positions`           | symbol_id (UQ)  | qty, avg_cost, unrealized_pnl, realized_pnl                                                                    | 当前持仓 + 累计盈亏         |
| `news_digest`         | id              | symbol_id, ts, source, title, url, summary, sentiment_score                                                    | 抓回的新闻 + AI 摘要       |
| `sentiment_snapshots` | (symbol_id, ts) | temperature, bullish_pct, bearish_pct, sample_size                                                             | 情绪温度时序，跨重启基线        |


`equity-monitor db status` 一键看各表行数。

### `signals.status` 状态机

```
pending  ──(execute_signal_trade)──►  executed   (executed_trade_id 关联到 trades.id)
   │                                       
   ├──(broker REJECTED)──►  cancelled
   └──(equity-monitor trade cancel)──►  cancelled
```

### `trades.status`

- `FILLED` — broker 已成交，`positions` 已更新
- `PENDING` — broker 接单但未成交（典型：闭市后下的 SIMULATE 单），写 trades 但**不**改 positions
- `REJECTED` — broker 拒单（不会写入 trades，只把 signals.status 标 cancelled）

---

## 故障排查

### 1. `Settings file not found: 'config/settings.yaml'`

不在 repo 根目录跑。`cd` 到 `equity-monitor/` 再跑，或加 `--settings /abs/path/to/settings.yaml`。

### 2. `equity-monitor chart --push` 报 `--image: --file must be a relative path`

`lark-cli ≥ 1.0.23` 要求图片是当前目录下的相对路径。代码已经处理（`cwd=path.parent` + 传 basename）。如果你看到这个错，多半是用了过老版本的 `equity-monitor` —— 拉最新代码。

### 3. 飞书听器收不到消息

WebSocket 后端要求：

- 飞书开发者后台「事件订阅」里**显式注册** `im.message.receive_v1`（订阅类型：长连接）
- 同一个 bot 全局只能有一个 WS 订阅者；重启前 `pkill -f 'lark-cli event'` 清掉孤儿进程
- 看 `equity-monitor listen` 日志有没有 `🟢 listener online`；没有就退到 `--backend polling`

### 4. `executed=0` 但 `suggestions=1`

可能原因（按概率）：

- **OpenDSecTrader 初始化失败**：找不到 SIMULATE 账户。在 OpenD 客户端登录时勾"模拟账户"
- **broker 拒单**：日志会有 `intraday_check.auto_execute_failed`，看 reason
- **重复信号**：之前已经执行过，信号 status≠pending（防止重复下单的内置守卫）
- **信号类型与策略 trigger 不匹配**：例如 RSI 超卖但没 MACD 金叉同时出现，`strategy_lite` 不会单独 BUY

`equity-monitor once --job intraday` 输出的 dict 配合 `data/equity_monitor.log` 看清楚。

### 5. 闭市后 trade 显示 `qty=100 px=0 status=PENDING`

正常。SIMULATE 在闭市后只接单不成交。开盘后会变 FILLED，`positions` 在那次成交回报后更新。

> ⚠️ 当前没有"成交回补"轮询任务（roadmap 项目）；如果闭市挂的 PENDING 单第二天开盘成交了但你下次 `intraday_check` 没碰到这只票，positions 会延迟更新。临时解法：手动 `once --job intraday` 触发一次下单同流程会重新对账。

### 6. 想推到群里而不是 DM

`config/settings.yaml` →

```yaml
lark:
  receiver:
    type: chat                      # 改成 chat
    open_id: "oc_xxxxxxxx"          # 群的 chat_id
```

获取群 chat_id：在该群里发条消息让 bot 接到，看日志里的 `chat_id` 字段；或 `lark-cli im +chats-list`。

### 7. 想用「我自己」的身份发消息

```bash
lark-cli auth login --scope im:message.send_as_user
```

然后 `settings.yaml` →

```yaml
lark:
  identity: user
```

---

## 测试 / 开发

```bash
pytest                              # 全套（约 3 秒）
pytest -m "not integration"         # 仅单测
pytest tests/integration/ -v        # 集成测试（FakeFutuClient + 内存 DB）
pytest -k auto_trade                # 只跑某模块
```

当前测试数：313（5 集成 + 3 单元为 Phase B 自动交易新增）。

### 项目结构

```
src/equity_monitor/
├── config.py                  pydantic v2 配置 + yaml loader
├── models.py                  SQLAlchemy 2.x ORM（8 张表）
├── db.py                      engine / sessionmaker / WAL pragma
├── futu_client.py             FutuClient Protocol + OpenDClient + FakeFutuClient
├── data/                      数据获取
│   ├── quotes.py              snapshot → quotes
│   ├── kline.py               K 线 → DataFrame
│   ├── indicators.py          RSI / MACD / Bollinger（纯 pandas/numpy，无 pandas-ta）
│   ├── tech_anomaly.py        富途技术异动 skill
│   ├── capital_anomaly.py     富途大单异动 skill
│   ├── news.py                富途新闻 skill
│   ├── sentiment.py           评论情绪 skill
│   └── backfill.py            历史 OHLC + 指标批量回填
├── signals/
│   ├── base.py                Signal + Severity
│   ├── threshold.py           价格阈值检测
│   ├── tech.py                RSI/MACD/Bollinger 状态切换检测
│   ├── compose.py             严重度提升 + 去重 + 拆分
│   └── strategy_lite.py       ★ 5 条 hard-coded 决策规则
├── trader/
│   ├── paper.py               PaperTrader Protocol + FakePaperTrader + OpenDSecTrader
│   └── execute.py             ★ execute_signal_trade（CLI / scheduler 共用）
├── scheduler/
│   ├── calendar.py            NYSE 交易日 / 早闭判断
│   ├── jobs.py                4 个 job + _execute_suggestions 自动下单
│   └── runner.py              APScheduler BlockingScheduler + cron 注册
├── reports/
│   ├── card.py                severity → 颜色 / emoji
│   ├── render.py              Jinja2 → 飞书卡片 JSON
│   ├── templates/*.j2         卡片模板
│   ├── lark.py                send_card via lark-cli + tenacity 重试
│   ├── snapshot.py            mplfinance K 线 PNG
│   └── lark_image.py          send_image via lark-cli + 重试
├── events/
│   ├── grammar.py             命令解析（斜杠 / 中文 / 自然语言）
│   ├── apply.py               命令执行（含 ChartCommand）
│   └── listener.py            飞书 WS / polling 主循环
└── cli/
    └── main.py                所有 click 子命令
```

### 加新策略 / 信号

- 新信号：在 `signals/` 下加 detector，让 `run_intraday_check` 加一条 `all_sigs.extend(...)`，并在 `compose.py` 注册严重度
- 新策略规则：在 `signals/strategy_lite.py` 里加规则；`triggering_signal_types` 决定它会绑定到哪个新插入的 `signals` 行去触发自动下单
- 新 Lark 命令：`events/grammar.py` 加 dataclass + parser，`events/apply.py` 加 handler

---

## 已知限制 & Roadmap

### 已知限制

- **没有 fill follow-up**：闭市后挂的 PENDING 单，开盘成交后 positions 不会被自动回补（要等下一次 `intraday_check` 触发同票流程，或手动 `once --job intraday`）
- **strategy_lite 是写死的规则**，不是 ML / LLM；要做策略对比 / 多策略并存还得抽 strategy registry
- **无 max-drawdown / equity curve 跟踪**：当前只有 `positions.realized_pnl` 这一个累积量
- **WebSocket 听器排他**：同一 bot 同时只能有一个 `lark-cli event` 订阅者；多窗格跑会丢消息
- **盘前 / 盘后行情不入库**：cron 只在 9:30–16:00 ET 工作日跑

### Roadmap（还没做）

1. **Fill confirmation pass** — 启动时 + 每个 cron tick 用 `position_list_query` 对账 PENDING 单的成交状态
2. **策略抽象层** — `Strategy` Protocol + Registry，支持多策略并存 + per-strategy P&L / max-drawdown 维护
3. `**/positions` `/pnl` `/history`** 飞书命令 + 专属卡片
4. **QuantStats tearsheet** — HTML 收益分析报告
5. **Web dashboard** — Streamlit / FastAPI + Plotly 网页可视化

---

## 许可

Internal — not for distribution.