# VibeLightTrader

A locally-hosted US-equity monitor + simulated autotrader. Every NYSE trading hour: pull quotes → compute indicators → run an LLM strategy → push a Lark card → place a paper-trade order on Futu's SIMULATE account. K-line snapshots, two-way Lark commands, position tracking, and P&L are all built in.

> **Architecture in one line**: a long-running `vibe-trader run` (scheduler) + an optional `vibe-trader listen` (Lark message listener) + a handful of one-shot CLI commands. State is persisted to SQLite, idempotent across restarts.

---

## Table of Contents

1. [Quick Start](#quick-start)
2. [What It Does](#what-it-does)
3. [How It Works](#how-it-works)
4. [Daily Operation](#daily-operation)
5. [Lark Command Reference](#lark-command-reference)
6. [CLI Reference](#cli-reference)
7. [Configuration](#configuration)
8. [Data Model](#data-model)
9. [Testing & Development](#testing--development)
10. [Known Limitations & Roadmap](#known-limitations--roadmap)

---

## Quick Start

```bash
# 0) Clone & enter the repo
git clone https://github.com/<you>/equity-monitor.git
cd equity-monitor

# 1) Python env  (do this BEFORE Step 2 — `check_opend.py` imports `futu`)
conda create -n vibe-trader python=3.11 -y
conda activate vibe-trader
pip install -e ".[dev]"

# 2) OpenD (Futu's local API daemon, one-time)
bash scripts/install_opend.sh        # prints the install + login walkthrough
python scripts/check_opend.py        # verifies 127.0.0.1:11111 + AAPL snapshot
# → Log into OpenD with your Futu account, switch the active account to SIMULATE.

# 3) cursor-agent  (only if you keep the default LLM strategy)
curl https://cursor.com/install | bash
cursor-agent login                   # OAuth in browser
cursor-agent status                  # → ✓ Logged in
# If you don't have a Cursor Pro/Max subscription, edit config/settings.yaml in
# Step 4 and set `trader.strategy.type: rule` — the project runs fine without LLM.

# 4) Lark Custom App + secret  (skip to keep Lark notifications off)
# 4a) Go to https://open.feishu.cn → 应用管理 → 创建企业自建应用
# 4b) 开发配置 → 权限管理, grant: im:message, im:message:send_as_bot,
#     im:resource, im:message.p2p_msg, im:message.p2p_msg:readonly
# 4c) 应用发布 → 创建版本 → 提交审核 (admin self-approves on personal tenants)
# 4d) Copy `App ID` (cli_xxx) into config/settings.yaml lark.app_id.
#     Export the matching `App Secret` as an env var:
export LARK_APP_SECRET='your-app-secret-here'
# Skip 4 entirely if you don't need Lark cards — the rest still runs.

# 5) Configuration
cp config/settings.example.yaml config/settings.yaml
cp config/watchlist.example.yaml config/watchlist.yaml
# Edit config/settings.yaml → fill in lark.app_id and lark.receiver.open_id.
#   (See "Finding your open_id" below if you don't know yours yet.)
# Edit config/watchlist.yaml → add the symbols you want to monitor.

# 6) Initialize DB + sync watchlist + backfill
vibe-trader db init
vibe-trader watchlist sync
vibe-trader backfill --days 30       # 30d of OHLC + indicators (recommended)

# 7) Run it
nohup vibe-trader run > var/scheduler.log 2>&1 &     # scheduler
# Optional second process for two-way Lark commands (skip if step 4 was skipped):
nohup vibe-trader listen > var/listener.log 2>&1 &   # message listener
```

Once it's running, on every NYSE trading day you'll see on Lark DM:

- An intraday card on every trading-hour `:30`, with K-line PNG + indicator readout + auto-fill receipt
- A morning brief at 10:30 ET, a closing summary at 16:30 ET

### Finding your open_id

Lark hides DM open_ids behind a privacy wall — there's no public lookup. Easiest path:

1. Start `vibe-trader listen` with a placeholder `lark.receiver.open_id` (anything starting with `ou_`).
2. DM your bot literally anything ("hi" works).
3. `tail -f var/listener.log` — the listener logs every received message; copy the `sender=ou_xxxxxxxx` value.
4. Stop the listener, paste that value into `lark.receiver.open_id`, restart.

(Or use the [Lark/Feishu OpenAPI explorer](https://open.feishu.cn/api-explorer) → `通讯录/contact.user/get` if you have an admin token handy.)

---

## What It Does

| Use case | How |
|---|---|
| **Monitor + auto-alert** | `vibe-trader run` runs in the background; price / RSI / MACD / Bollinger / threshold breaches all push Lark cards |
| **Auto paper-trading** | On by default. Each cron tick that produces a BUY/SELL suggestion places an order on Futu SIMULATE and records a `Trade` row |
| **K-line snapshot** | `/chart US.AAPL D` from Lark, or `vibe-trader chart US.NVDA --freq 60m --push` from the shell. Marks buy/sell points + cost-basis line |
| **Edit watchlist** | DM the bot: `添加 US.AAPL 上限200 下限165`, `阈值 NVDA 上限150`, `删除 TSLA`, `列表` |
| **Inspect history / positions / P&L** | `vibe-trader trade positions` / `pnl --days 7` / `list` |
| **Manual confirm/cancel** | When `auto_execute=false`: `vibe-trader trade confirm <signal_id>` / `cancel <signal_id>` |
| **Backfill / inspect** | `vibe-trader backfill --days N` / `vibe-trader db status` |

### Signal types (rendered in alert cards)

| Signal | Severity | Source |
|---|---|---|
| `threshold_breach_upper` / `_lower` | CRITICAL | User-defined upper/lower thresholds in watchlist |
| `rsi_overbought` / `_oversold` | WARN | RSI(14) crossing 70 / 30 |
| `macd_golden_cross` / `_death_cross` | INFO / WARN | MACD line crossing the signal line |
| `boll_upper_break` / `_lower_break` | INFO | Close crossing Bollinger band |

`signals.dedupe_window_minutes` (default 60) deduplicates same-type signals within the window.

### Strategy layer

Four strategies share one Protocol; pick one in `config/settings.yaml` → `trader.strategy.type`:

| Strategy | When to use | Key trait |
|---|---|---|
| `llm` *(default)* | Production. Lets an LLM weigh signals + position + investor profile | `provider: cursor-agent` ⇒ uses your Cursor Pro/Max subscription, **no separate API key** |
| `rule` | Explicit fallback / when you want fully deterministic behavior | 5 hard-coded rules in `signals/strategy_rule.py` |
| `hitl` | Manual review of every trade | Writes a Markdown decision packet, you reply via `vibe-trader decide submit` |
| `ensemble` | Multi-strategy voting | Skeleton in place, not yet wired |

#### `provider: cursor-agent` — using your Cursor subscription as the LLM backend

```bash
# One-time setup
curl https://cursor.com/install | bash
cursor-agent login            # OAuth in browser
cursor-agent status           # → ✓ Logged in as <your account>
```

```yaml
# config/settings.yaml
trader:
  strategy:
    type: llm
    llm:
      provider: cursor-agent
      model: ""               # "" = account default; or "sonnet-4" / "gpt-5"
      timeout_s: 240          # CLI takes 30-60s; leave headroom
      max_position_per_symbol: 200
      min_trade_size: 10
      min_confidence: 0.6     # below this, suggestion is auto-demoted to HOLD
      fallback_on_error: rule # on CLI timeout / parse failure / constraint violation
```

Every cron tick spawns `cursor-agent -p '<prompt>' --output-format json`. Each decision is appended to `data/llm_decisions.jsonl` (NDJSON, append-only) for audit.

`enforce_constraints` runs as a second-line guard: max-position, qty floor, low confidence — all are checked after the LLM returns. Anything that fails routes through `fallback_on_error`.

To switch backends, change one line:

```yaml
provider: anthropic            # then export ANTHROPIC_API_KEY=...
```

```yaml
provider: openai_compat        # DeepSeek / Doubao / OpenRouter / Ollama
base_url: https://api.deepseek.com
api_key_env: DEEPSEEK_API_KEY
```

#### `type: hitl` — human-in-the-loop review

Useful when you want to review every trade, or want to validate a prompt before going fully automated:

1. Set `trader.strategy.type: hitl`
2. On each event, vibe-trader writes a Markdown decision packet to `var/decisions/pending/<id>.md` and pushes a Lark card with the packet ID
3. You open Cursor → ask Claude to read the packet → Claude returns a fixed-schema JSON
4. Submit it back: `vibe-trader decide submit <id> --json '<paste>'`

`vibe-trader decide` subcommands:

```text
decide list  [--state pending|submitted|executed|cancelled|all]
decide show  <packet_id>
decide submit <packet_id> --json '...' | --file decision.json [--no-execute]
decide cancel <packet_id> [--reason "..."]
```

### Investor profile (medium-term framing)

Independent of which strategy you pick, a single block in `settings.yaml` parameterizes the user's intent and is fed into every LLM prompt:

```yaml
trader:
  investment_profile:
    enabled: true
    horizon_months_min: 3
    horizon_months_max: 6
    style: growth                    # growth | value | blend | income | speculative
    budget_per_symbol_usd: 50000
    drawdown_tolerance_pct: 20
    initial_entry_pct: 40            # first buy = 40% of budget
    max_batches: 3                   # max accumulating buys
    add_on_dip_pct: 5                # next add-on requires ≥5% dip
    take_profit_pct: 30              # +30% triggers trim
    take_profit_trim_pct: 50
    hard_stop_pct: 20                # hard SELL on -20%
    min_holding_days: 30             # block voluntary SELL within N days of buy
```

### Safety guardrails

- **SIMULATE-only**: `OpenDSecTrader` actively scans for a SIMULATE account at startup and refuses to operate without one. It will never touch a real-money account.
- **Error isolation**: a single symbol's order rejection doesn't affect others; `OpenDSecTrader` init failure disables auto-trading for the round but quote alerts continue
- **Idempotent**: same `(symbol, ts, signal_type)` is only ordered once; scheduler restarts and cron repeats won't double-fire
- **PENDING orders don't pollute positions**: an after-hours SIMULATE order lands as PENDING — recorded in `trades` but **not** applied to `positions` until the broker confirms the fill (avoids the `qty=100 / avg_cost=0` pollution case)

---

## How It Works

```
                                           ┌───────────────────────────┐
       APScheduler                          │   Futu OpenD :11111       │
       (NYSE calendar)                      │   quotes + SIMULATE trade │
            │                               └─────────┬─────────────────┘
            ▼                                         │
  every :30 of trading hour    ┌────────────────────┐ │
  intraday_check  ───────────► │ run_intraday_check │◄┘
                               │  ① snapshot + K线   │
                               │  ② RSI/MACD/BOLL   │
                               │  ③ signals          │
                               │  ④ Strategy.decide │ ← LLM / rule / hitl
                               │  ⑤ persist Signal  │
                               │  ⑥ auto-execute   ─┐│
                               │  ⑦ render card+PNG ││
                               └─────┬───────────────┘│
                                     │  Lark card+PNG │
                                     ▼                │
                               ┌──────────┐           │
                               │ lark-cli │           │
                               └────┬─────┘           ▼
                                    │       ┌────────────────────┐
                                    ▼       │ trader/execute.py  │
                              Lark DM /     │ Trade + Position   │
                              group chat    │ → SQLite           │
                                            └────────────────────┘
                              ▲
                              │
                        ┌──────────┐
                        │ listener │ ◄─── Lark WS event
                        │ /add     │      (im.message.receive_v1)
                        └──────────┘
```

Three long-running components:

- **`vibe-trader run`** — APScheduler, fires 4 cron jobs
- **`vibe-trader listen`** — optional Lark listener for two-way commands
- **OpenD** (Futu) — must be running first; provides quotes + SIMULATE trading

### Cron schedule (NYSE timezone)

| Job | Cron | Purpose |
|---|---|---|
| `intraday_check` | `30 9-15 * * mon-fri` | Every :30 of trading hour: snapshot + indicators + signals + **auto-trade** + card + K-line |
| `morning_brief` | `30 10 * * mon-fri` | 1h after open: gainers/losers leaderboard |
| `closing_brief` | `30 16 * * mon-fri` | After close: daily summary |

NYSE holidays (incl. early close) and DST are handled by `pandas-market-calendars` — non-trading days are skipped wholesale.

---

## Daily Operation

### Start / stop

```bash
# Recommended: nohup + scheduler.log
nohup vibe-trader run > var/scheduler.log 2>&1 &
nohup vibe-trader listen > var/listener.log 2>&1 &

# Stop
ps aux | grep vibe-trader     # find PIDs
kill <pid>                    # SIGTERM is graceful
```

Or use `tmux` for interactive sessions if you prefer.

### Run a single tick (without affecting the scheduler)

```bash
vibe-trader once --job intraday              # honors cfg.trader.auto_execute
vibe-trader once --job intraday --no-auto-trade   # force no orders (dry-run)
vibe-trader once --job intraday --auto-trade      # force orders (override config)
vibe-trader once --job morning
vibe-trader once --job closing
```

The return value is a dict, e.g.:

```
{'quotes': 3, 'signals': 4, 'pushed': 3, 'suggestions': 1, 'executed': 1}
```

- `quotes` — newly inserted `quotes` rows
- `signals` — deduped signals produced this round
- `pushed` — Lark cards pushed
- `suggestions` — non-HOLD strategy decisions
- `executed` — orders that actually placed (0 when `auto_execute=false` or no paper trader)

### Inspect state

```bash
vibe-trader db status                        # row counts per table
vibe-trader watchlist list                   # active symbols in DB
vibe-trader trade positions                  # current positions + unrealized P&L
vibe-trader trade pnl --days 7               # realized P&L over the window
vibe-trader trade list --status pending      # pending suggestions (when auto_execute=false)
vibe-trader trade list --status executed     # filled trades
```

### Disable auto-trading temporarily

```bash
# Persistent: set trader.auto_execute: false in settings.yaml, restart `run`.
# Per-tick:
vibe-trader once --job intraday --no-auto-trade
```

When `auto_execute=false`, the manual confirm flow:

```bash
vibe-trader trade list --status pending      # see suggestions
vibe-trader trade confirm 7                  # confirm (add --qty 50 to override size)
vibe-trader trade cancel 7                   # cancel a pending suggestion
```

### Edit watchlist (no restart needed)

DM the bot:

```
添加 US.AAPL 上限200 下限165
阈值 US.NVDA 上限150 下限110
删除 TSLA
列表
```

Or edit `config/watchlist.yaml` and run `vibe-trader watchlist sync`.

---

## Lark Command Reference

`vibe-trader listen` must be running. Commands accept slash, Chinese, and natural-language style. **Only the user behind `lark.receiver.open_id`** can edit the watchlist (other senders are ignored).

| Operation | Examples |
|---|---|
| Add | `添加 US.AAPL 上限200 下限165` / `/add US.AAPL upper=200 lower=165` / `监控 TSLA` |
| Remove | `删除 US.AAPL` / `取消 AAPL` / `/remove US.AAPL` |
| Update threshold | `阈值 US.AAPL 上限205` / `/threshold US.AAPL upper=205 lower=170` |
| List | `列表` / `/list` |
| Chart | `/chart US.AAPL` / `/chart AAPL D` / `图 TSLA` / `chart NVDA 15m` |
| Help | `帮助` / `/help` |

`/chart` supports frequencies `5m`, `15m`, `30m`, `60m` (default), `D`, `W`. `1m` is intentionally not exposed (too noisy).

### Listener backend

```bash
vibe-trader listen                                          # default: websocket
vibe-trader listen --backend polling --poll-interval 10     # polling fallback
vibe-trader listen --rich-cards                             # default ON: live price + indicators
vibe-trader listen --text-only                              # plain markdown reply
```

The websocket backend requires the bot's app on Lark to have `im.message.receive_v1` registered as a long-poll subscription, and only one process can subscribe at a time.

---

## CLI Reference

```
vibe-trader [--settings PATH] [--watchlist PATH]
├── run                                 start the long-running scheduler (blocking)
├── listen                              start the Lark message listener (blocking)
│     [--backend websocket|polling] [--poll-interval N] [--rich-cards/--text-only]
├── once --job intraday|morning|closing
│                                       run one job and print the result dict
│     [--auto-trade|--no-auto-trade]    override cfg.trader.auto_execute (intraday only)
├── analyze --code CODE [...]           run an on-demand LLM analysis (no signal trigger required)
│     [--execute]                       auto-execute BUY/SELL decisions
├── backfill [--days N]                 backfill 60-min OHLC + indicators (default 30d, idempotent)
│
├── chart TICKER                        render a K-line snapshot PNG (optionally push to Lark)
│     [--freq 60m|5m|15m|30m|D|W]       (default 60m)
│     [--out-dir PATH]                  (default var/snapshots)
│     [--push|--no-push]                (default --no-push)
│     [--no-reconcile]                  skip the broker fill-price backfill step
│
├── watchlist
│   ├── list                            list active symbols in DB
│   └── sync                            upsert config/watchlist.yaml into the symbols table
│
├── trade
│   ├── list [--status pending|confirmed|executed|cancelled|all]
│   ├── confirm SIGNAL_ID [--qty N]
│   ├── cancel SIGNAL_ID
│   ├── positions
│   └── pnl [--days N]
│
├── decide                              HITL strategy commands (see `## Strategy layer`)
│   ├── list / show / submit / cancel
│
└── db
    ├── init                            create the SQLite schema
    └── status                          row counts per table
```

`--settings` / `--watchlist` default to `config/{settings,watchlist}.yaml` relative to cwd. Most commands need to be run from the repo root (config files are resolved from cwd by default); use absolute paths if running elsewhere.

---

## Configuration

### `config/settings.yaml`

```yaml
opend:
  host: 127.0.0.1
  port: 11111

database:
  path: data/vibe_trader.db
  wal_mode: true                         # SQLite WAL; better concurrent reads

scheduler:
  timezone: America/New_York
  jobs:
    intraday_check: { cron: "30 9-15 * * mon-fri" }
    morning_brief:  { cron: "30 10 * * mon-fri" }
    closing_brief:  { cron: "30 16 * * mon-fri" }

lark:
  app_id: cli_xxxxxxxxxxxxxxxx           # from open.feishu.cn → 应用配置 → 凭证与基础信息
  app_secret_env: LARK_APP_SECRET        # env var holding the App Secret (do NOT inline)
  base_url: https://open.feishu.cn       # Lark international: https://open.larksuite.com
  receiver:
    type: user                           # user → DM via open_id; chat → group via chat_id
    open_id: "ou_xxx..."

signals:
  rsi_overbought: 70
  rsi_oversold: 30
  bollinger_period: 20
  bollinger_std: 2
  macd_fast: 12
  macd_slow: 26
  macd_signal: 9
  dedupe_window_minutes: 60              # same-type signal dedupe window

logging:
  level: INFO
  file: data/vibe_trader.log

trader:
  auto_execute: true                     # auto-place SIMULATE orders (default ON)
  simulate_only: true                    # always true; refuses non-SIMULATE accounts
  strategy:
    type: llm                            # llm | rule | hitl | ensemble
    # ... per-strategy blocks; see "Strategy layer"
  investment_profile:
    # ... see "Investor profile"
```

### `config/watchlist.yaml`

```yaml
symbols:
  - code: US.AAPL                        # must be prefixed: US./HK./SH./SZ.
    name: Apple
    upper_threshold: 200.0               # close > upper → CRITICAL → SELL all
    lower_threshold: 165.0               # close < lower → CRITICAL → BUY 100
    notes: "core position"
  - code: US.NVDA
    name: NVIDIA
    upper_threshold: 220.0
    lower_threshold: 170.0
  - code: US.TSLA                        # no thresholds → tech-only signals (RSI/MACD/BOLL)
    name: Tesla
```

Run `vibe-trader watchlist sync` (or DM `/add` from Lark) for changes to take effect.

---

## Data Model

SQLite, 6 tables, all created by `vibe-trader db init`:

| Table | PK | Key columns | Purpose |
|---|---|---|---|
| `symbols` | id | code, name, upper_threshold, lower_threshold, is_active | Watchlist mirror |
| `quotes` | id | symbol_id, ts, last_price, open/high/low, volume | Realtime snapshot history |
| `indicators` | (symbol_id, ts) | rsi_14, macd, macd_signal, macd_hist, boll_* | Per-bar indicator values |
| `signals` | id | symbol_id, ts, signal_type, severity, payload_json, suggested_action, suggested_qty, status, executed_trade_id | Each signal + its strategy decision + lifecycle state |
| `trades` | id | symbol_id, ts, side, qty, price, futu_order_id, signal_id, status (FILLED/PENDING/REJECTED) | Paper-trade history |
| `positions` | symbol_id (UQ) | qty, avg_cost, unrealized_pnl, realized_pnl | Current positions + P&L |

`vibe-trader db status` prints row counts per table.

### `signals.status` state machine

```
pending  ──(execute_signal_trade)──►  executed   (executed_trade_id → trades.id)
   │
   ├──(broker REJECTED)──►  cancelled
   └──(vibe-trader trade cancel)──►  cancelled
```

### `trades.status`

- `FILLED` — broker confirmed; `positions` updated
- `PENDING` — broker accepted but not yet filled (typical for after-hours SIMULATE orders); `trades` written but `positions` left untouched. The `chart` command opportunistically reconciles these on the next run via `reconcile_pending_fills`.
- `CANCELLED` — order cancelled at broker side (post-fact reconcile)
- `REJECTED` — broker rejected; **not** written to `trades`, only `signals.status` becomes `cancelled`

---

## Testing & Development

```bash
pytest tests/unit -q                      # unit tests (~5s)
pytest tests/integration -v               # integration tests (FakeFutuClient + in-memory DB)
pytest -k auto_trade                      # filter by name
```

Current count: **487 unit tests + 24 integration tests**, all passing.

### Project structure

```
src/vibe_trader/
├── config.py                  Pydantic v2 config + yaml loader
├── models.py                  SQLAlchemy 2.x ORM (6 tables)
├── db.py                      engine / sessionmaker / WAL pragma
├── futu_client.py             FutuClient Protocol + OpenDClient + FakeFutuClient
├── analyze.py                 on-demand LLM analysis (vibe-trader analyze)
├── data/
│   ├── quotes.py              snapshot → quotes
│   ├── kline.py               K-line → DataFrame
│   ├── indicators.py          RSI / MACD / Bollinger (pure pandas/numpy)
│   └── backfill.py            historical OHLC + indicator batch backfill
├── signals/
│   ├── base.py                Signal + Severity
│   ├── threshold.py           price threshold detector
│   ├── tech.py                RSI/MACD/Bollinger state-transition detectors
│   ├── compose.py             dedupe + severity escalation
│   ├── strategy_base.py       Strategy Protocol + Registry
│   ├── strategy_lite.py       SignalSuggest dataclass
│   ├── strategy_rule.py       deterministic 5-rule strategy
│   ├── strategy_llm.py        LLMStrategy with constraint guard + audit log + fallback
│   └── strategy_hitl.py       human-in-the-loop strategy
├── llm/
│   ├── client.py              LLMClient Protocol + LLMResponse + error hierarchy
│   ├── prompt.py              Jinja2 prompt templates + JSON-tolerant parse_decision
│   ├── factory.py             build_llm_client(provider=...)
│   ├── cursor_agent.py        cursor-agent CLI backend
│   ├── anthropic_client.py    Anthropic API backend
│   └── openai_compat.py       OpenAI / DeepSeek / Doubao / Ollama / OpenRouter
├── lark/
│   ├── auth.py                tenant_access_token cache (TokenManager)
│   ├── client.py              LarkHTTPClient: send_card / send_text / send_image / list_messages
│   └── errors.py              LarkAPIError
├── trader/
│   ├── paper.py               PaperTrader Protocol + FakePaperTrader + OpenDSecTrader
│   ├── execute.py             execute_signal_trade (CLI / scheduler share)
│   └── reconcile.py           reconcile_pending_fills (backfill MARKET-order fills)
├── decisions/                 HITL packet write/read
├── journal/                   per-symbol Markdown journal + hit-rate metrics + dev_log
├── scheduler/
│   ├── calendar.py            NYSE trading-day / early-close logic
│   ├── jobs.py                3 cron jobs + auto-execution
│   └── runner.py              APScheduler BlockingScheduler
├── reports/
│   ├── card.py                severity → color/emoji
│   ├── render.py              Jinja2 → Lark card JSON
│   ├── templates/*.j2         card templates
│   ├── lark.py                send_card via lark-cli + tenacity retry
│   ├── snapshot.py            mplfinance K-line PNG
│   └── lark_image.py          send_image via lark-cli + retry
├── events/
│   ├── grammar.py             command parsing (slash / Chinese / natural-language)
│   ├── apply.py               command execution (incl. ChartCommand)
│   └── listener.py            Lark WS / polling main loop
└── cli/
    └── main.py                all click subcommands
```

### Adding a new strategy / signal

- **New signal**: add a detector under `signals/`, append it in `run_intraday_check`, register severity in `compose.py`
- **New strategy**: implement the `Strategy` Protocol in a new module, register via `@register_strategy("your-name")`, add a sub-block under `trader.strategy` in YAML
- **New Lark command**: add a dataclass + parser to `events/grammar.py`, add a handler to `events/apply.py`

---

## Known Limitations & Roadmap

### Known limitations

- **Listener latency is 3–10s (HTTP polling)**: the previous build supported a WebSocket subscriber via an internal Node tool, but the public path is HTTP-only. Polling adapts to a 3s window after each user message and idles at 10s — fine for a personal chat-controlled tool, not for high-frequency automation. A real WebSocket / webhook path is on the Roadmap.
- **PENDING-fill reconcile is opportunistic, not background**: `reconcile_pending_fills` runs at the start of every `chart` command. After-hours PENDING orders that fill overnight don't get back-filled into `positions` until you next render a chart or the next intraday tick produces a fresh decision on the same symbol.
- **No backtest framework**: medium-term LLM strategies don't backtest cleanly anyway (LLM behavior drifts), but a minimal version would still help.
- **No portfolio-level risk**: per-symbol stop-loss / take-profit exists; correlation across symbols, total exposure, and equity curve are not tracked.
- **Pre/post market quotes are not persisted**: cron runs only 9:30–16:00 ET on trading days.
- **LLM decisions are non-deterministic**: same prompt may produce different outputs across runs. Use `data/llm_decisions.jsonl` for post-hoc auditing.

### Roadmap

1. **Background fill confirmation** — periodic `position_list_query` reconcile loop, not just on `chart`
2. **Portfolio-level guards** — concentration limits, correlation caps, equity-curve drawdown stop
3. **`/positions` `/pnl` `/history` Lark cards** — first-class commands instead of having to use the CLI
4. **News & sentiment input** — currently only indicators + profile go to the LLM; pulling real news (Futu / yfinance / FMP) and comment sentiment back in would help. The earlier scaffolding around four `data/*` skills was removed — see git history if you want to retry with a real provider.
5. **QuantStats tearsheet** — HTML P&L analysis report
6. **Web dashboard** — Streamlit / FastAPI + Plotly
7. **Real-money RFC** — what would need to change to flip `simulate_only: false`

---

## License

MIT. See [LICENSE](./LICENSE).

## Buy Me A Coffee ☕️

<p align="center">
  <img src="https://i.imgs.ovh/2026/05/05/f6269338cd42e4ce5f92f8fb0d882045.png" alt="Buy Me A Coffee" width="280">
</p>