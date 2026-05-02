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
- DB-backed sentiment baseline survives runner restarts (`sentiment_snapshots`)
- 4 cron jobs: `intraday_check`, `morning_brief`, `closing_brief`, `news_pulse`
- NYSE calendar gating (holidays + DST handled by `pandas-market-calendars`)
- Lark Interactive Card rendering via Jinja2 + push via `lark-cli`
- Backfill historical OHLC + indicators idempotently
- 126 unit + integration tests, all green

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

Find your Lark `open_id`:

```bash
lark-cli contact +me
```

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
```

### 6. Run forever

```bash
tmux new -s equity
conda activate fin
equity-monitor run
# Ctrl-B D to detach; Ctrl-B kill-session to fully stop
```

## CLI Reference

```
equity-monitor [--settings PATH] [--watchlist PATH]
├── run                                Start the long-running scheduler.
├── once --job intraday|morning|closing|news
│                                      Run a single job once and print result.
├── backfill [--days N]                Backfill 60-min OHLC + indicators (default 30 days).
├── watchlist
│   ├── list                           List active symbols in DB.
│   └── sync                           Upsert config/watchlist.yaml → symbols table.
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
pytest                                 # all 126 tests (~3s)
pytest -m "not integration"            # unit only (~1.5s)
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
  cli/
    main.py                  click subcommands (run/once/backfill/watchlist/db)
```

## Architecture

See `docs/superpowers/specs/2026-05-02-equity-monitor-design.md` for the full
design spec, including the data-flow diagram, SQLite schema, signal fusion
rules, and Phase 2/3 roadmap (paper trading, fully-auto execution).

## License

Internal — not for distribution.
