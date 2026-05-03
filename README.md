# Equity Monitor

Hourly US-equity monitor with technical signals, news sentiment, and Lark alerts.

A long-running Python service that pulls quotes / 60-min K-line / RSI / MACD /
Bollinger every NYSE trading hour, fuses Futu Technical & Capital anomaly
signals plus comment-sentiment bursts, and pushes structured Lark Interactive
Cards via `lark-cli`.

## Features

- Real-time snapshot + 60-min K-line, persisted to SQLite (`quotes`, `indicators`)
- Self-implemented RSI(14) (Wilder), MACD(12/26/9), Bollinger Bands(20, 2σ) — no `pandas-ta`
- Futu skill integrations: technical anomaly, capital anomaly, news, comment sentiment
- Multi-source signal fusion + dedupe + severity split (INFO / WARN / CRITICAL)
- **Card diagnostics block** — RSI/MACD/BOLL current values + Chinese interpretation, intraday + 30-bar return %, position P&L
- **Phase 2:** rule-based suggested actions (BUY/SELL with quantity), `equity-monitor trade` CLI for paper-trading confirmation, P&L summary in briefs
- **Phase 2.5:** Lark message-driven watchlist control (`/add`, `/remove`, `/list`, `/threshold`, plus Chinese natural-language equivalents)
- **Phase 3 (scoped):** K-line snapshot PNG with trade markers / cost line / live price; auto-attached after intraday Lark cards when configured; on-demand `/chart` in Lark + `equity-monitor chart`
- DB-backed sentiment baseline survives runner restarts (`sentiment_snapshots`)
- 4 cron jobs: `intraday_check`, `morning_brief`, `closing_brief`, `news_pulse`
- NYSE calendar gating (holidays + DST handled by `pandas-market-calendars`)
- Lark Interactive Card rendering via Jinja2 + push via `lark-cli`
- Backfill historical OHLC + indicators idempotently

## Phase 3 (scoped) — K-line snapshot visualization

**TL;DR**: Every signal alert can carry a static PNG snapshot of the K-line
(default **60-minute** bars), with BUY/SELL paper-trade markers overlaid, the
position’s average cost as an orange dashed line, and the live price as a
steel-blue dashed line. You can request the same snapshot on demand with
`/chart <code> [freq]` in Lark or `equity-monitor chart <code>` in the shell.

### What landed

1. **Snapshot renderer** (`equity_monitor.reports.snapshot.render_snapshot`)
   uses mplfinance to draw OHLCV + markers + reference lines and writes a
   PNG under `var/snapshots/` (or a custom `--out-dir`).
2. **Lark image sender** (`equity_monitor.reports.lark_image.send_image`)
   wraps `lark-cli im +messages-send --image <abs-path>` with the same
   retry / error contract as the existing card sender (`reports/lark.py`).
3. **Multi-frequency K-line support**: `5m`, `15m`, `30m`, `60m`, `D`, `W`
   are valid `--freq` values. `1m` is intentionally excluded (too noisy
   for visual inspection).
4. **Auto-attach to alerts**: `run_intraday_check` sends the snapshot
   automatically after each successful card push when both `send_image_fn`
   and `snapshot_dir` are configured. Image send is **non-fatal** —
   failures are logged but do not block subsequent alerts.
5. **`/chart` on-demand** in Lark: `/chart US.AAPL`, `/chart AAPL D`,
   `图 TSLA`. Available on both websocket and polling listener backends.
6. **`equity-monitor chart` CLI**: `equity-monitor chart US.AAPL --freq 60m
   [--push]` for ad-hoc rendering / sharing from the terminal.

### Quick smoke test

```bash
conda activate fin
python scripts/smoke_phase3.py        # render-only (no Lark traffic)
python scripts/smoke_phase3.py --push # also send the PNG to your Lark
```

### What’s NOT in Phase 3 (deferred)

- Strategy abstraction layer (per-strategy P&L, max drawdown, equity curves).
- `/positions`, `/pnl`, `/history` listener commands and their dedicated card
  rendering (beyond existing brief summaries).
- QuantStats tearsheet.
- `BackfillState` cursor for incremental K-line pulls.
- An interactive web dashboard (Streamlit / Plotly Dash).

These may arrive in a later phase if needed.

## Quickstart

### 1. Install OpenD (one-time)

```bash
bash scripts/install_opend.sh   # follow prompts; logs you into Futu OpenD
python scripts/check_opend.py   # verify OpenD reachable on 127.0.0.1:11111
```

### 2. Configure

```bash
cp config/watchlist.example.yaml config/watchlist.yaml
# edit config/watchlist.yaml — pick your symbols (US.AAPL, US.NVDA, ...) and price thresholds
# edit config/settings.yaml — set lark.receiver.open_id to your Lark open_id
```

Find your Lark `open_id` (omit `--user-id` to query yourself):

```bash
lark-cli contact +get-user | jq -r '.data.user.open_id'
```

> **Note on `lark.identity`** — Default `bot` works without extra setup (your
> equity-monitor bot DMs you the cards). To send messages **as your own user
> identity** instead, run
> `lark-cli auth login --scope "im:message.send_as_user"` and set
> `lark.identity: user` in `config/settings.yaml`.

### 3. Set up Python env + initialize DB

```bash
conda create -n fin python=3.11 -y    # one-time
conda activate fin
pip install -e ".[dev]"

equity-monitor db init        # create SQLite schema
equity-monitor watchlist sync # upsert config/watchlist.yaml → DB
```

### 4. Backfill historical data (optional but recommended)

```bash
equity-monitor backfill --days 30
# pulls ~7 K_60M bars per US trading day, computes indicators, writes both
```

### 5. Smoke test (optional, requires OpenD + lark-cli)

```bash
python scripts/smoke_e2e.py
# Verify 4 Lark cards arrive in your IM (intraday / morning / closing / news pulse)

# K-line snapshot pipeline (Phase 3; needs OpenD + DB; add --push for lark-cli send)
python scripts/smoke_phase3.py
```

### 6. Run forever

```bash
tmux new -s equity
conda activate fin
# pane 1: scheduler
equity-monitor run
# Ctrl-B " then in pane 2:
equity-monitor listen
# Ctrl-B D to detach; tmux kill-session -t equity to fully stop
```

## Lark message control

Once `equity-monitor listen` is up you can DM the bot in Lark to manage the
watchlist and request `/chart` snapshots. Slash-style commands, Chinese aliases,
and natural-language phrases work:

| Action | Examples |
|---|---|
| Add | `添加 US.AAPL 上限200 下限165` / `/add US.AAPL upper=200 lower=165` / `监控 TSLA` |
| Remove | `删除 US.AAPL` / `取消 AAPL` / `/remove US.AAPL` |
| Update thresholds | `阈值 US.AAPL 上限205` / `/threshold US.AAPL upper=205 lower=170` |
| List | `列表` / `/list` |
| Chart (K-line PNG) | `/chart US.AAPL` / `/chart AAPL D` / `图 TSLA` |
| Help | `帮助` / `/help` |

Sender is gated by `lark.receiver.open_id` — only your configured account can
mutate the watchlist (other senders are ignored).

## CLI Reference

```
equity-monitor [--settings PATH] [--watchlist PATH]
├── run                                Start the long-running scheduler.
├── listen                             Start the Lark message listener.
├── once --job intraday|morning|closing|news
│                                      Run a single job once and print result.
├── backfill [--days N]                Backfill 60-min OHLC + indicators (default 30 days).
├── watchlist
│   ├── list                           List active symbols in DB.
│   └── sync                           Upsert config/watchlist.yaml → symbols table.
├── trade
│   ├── list [--status pending|...]    Show pending suggestions.
│   ├── confirm SIGNAL_ID [--qty N]    Place paper-trade order for a suggestion.
│   ├── cancel SIGNAL_ID               Mark suggestion as cancelled.
│   ├── positions                      List current paper positions.
│   └── pnl [--days N]                 Realized P&L by symbol.
├── chart CODE [--freq 60m|5m|...] [--out-dir ...] [--push]
│                                      Render K-line snapshot PNG (+ optional Lark).
└── db
    ├── init                           Create SQLite schema.
    └── status                         Print row counts of all tables.
```

Both `--settings` and `--watchlist` default to `config/{settings,watchlist}.yaml`
under the current working directory. Configs are loaded lazily, so `--help`
works even before they exist.

## Scheduling

| Job              | Cron (America/New_York)        | Purpose                                            |
|------------------|--------------------------------|----------------------------------------------------|
| `intraday_check` | `30 9-15 * * mon-fri`          | Hourly: snapshot + indicators + signals + alerts.  |
| `morning_brief`  | `30 10 * * mon-fri`            | One hour after open: gainers/losers digest card.   |
| `closing_brief`  | `30 16 * * mon-fri`            | After close: daily wrap card.                      |
| `news_pulse`     | `*/30 9-15 * * mon-fri`        | Every 30 min: news + sentiment burst detection.    |

NYSE holidays (incl. early closes) and DST switches are handled automatically
via `pandas-market-calendars`. Non-trading days are skipped before the
scheduler dispatches the job.

## Signals

| Signal                          | Severity | Source                                |
|---------------------------------|----------|---------------------------------------|
| `threshold_breach_upper/lower`  | CRITICAL | User-defined per-symbol thresholds.   |
| `rsi_overbought` / `oversold`   | WARN     | RSI(14) crosses 70 / 30.              |
| `macd_golden_cross`             | INFO     | MACD line crosses above signal.       |
| `macd_death_cross`              | WARN     | MACD line crosses below signal.       |
| `boll_upper_break` / `lower`    | INFO     | Close pierces Bollinger band.         |
| `futu_tech_anomaly`             | INFO/CRIT| Promoted to CRITICAL on reversal pat. |
| `futu_capital_anomaly`          | WARN     | Large institutional flow.             |
| `news_pulse_pos` / `neg`        | WARN     | Sentiment temp delta ≥ 3.0.           |

Duplicates within `dedupe_window_minutes` (default 60) are dropped before
push.

## Testing

```bash
pytest                                 # full suite (~3s); 301 tests as of Phase 3
pytest -m "not integration"            # unit-only slice
pytest tests/integration/ -v           # integration with FakeFutuClient + in-mem DB
```

## Project Layout

```
src/equity_monitor/
  config.py                  pydantic v2 models + yaml loaders
  models.py                  SQLAlchemy 2.x ORM (8 tables)
  db.py                      engine / sessionmaker / WAL pragma
  futu_client.py             FutuClient Protocol + OpenDClient + FakeFutuClient
  data/
    quotes.py                snapshot → quotes
    kline.py                 K-line → DataFrame
    indicators.py            RSI / MACD / Bollinger (pure pandas/numpy)
    tech_anomaly.py          Futu skill subprocess wrapper
    capital_anomaly.py       Futu skill subprocess wrapper
    news.py                  Futu skill subprocess wrapper
    sentiment.py             Futu skill subprocess wrapper
    backfill.py              historical OHLC + indicators bulk-load
  signals/
    base.py                  Signal + Severity
    threshold.py             user-defined price-threshold detection
    tech.py                  RSI/MACD/Bollinger transition detection
    compose.py               severity upgrade + dedupe + split
  scheduler/
    calendar.py              NYSE trading days, market open, early close
    jobs.py                  intraday_check, morning/closing brief, news_pulse
    runner.py                APScheduler BlockingScheduler + cron triggers
  reports/
    card.py                  severity → color/emoji mapping
    render.py                Jinja2 → Lark Interactive Card JSON
    templates/*.j2           card templates (signal_alert / daily_brief / news_pulse)
    lark.py                  send_card via lark-cli subprocess + tenacity retry
    snapshot.py              mplfinance K-line PNG snapshots
    lark_image.py            send_image via lark-cli (--image), tenacity retry
  cli/
    main.py                  click subcommands (run/listen/once/chart/backfill/watchlist/trade/db)
```

## Architecture

See `docs/superpowers/specs/2026-05-02-equity-monitor-design.md` for the full
design spec, including the data-flow diagram, SQLite schema, signal fusion
rules, and backlog for paper trading automation and dashboards.

## License

Internal — not for distribution.
