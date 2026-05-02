# Equity Monitor — Phase 2 Spec: Semi-Auto Paper Trading

**Status:** Draft (auto-generated 2026-05-02)
**Builds on:** `2026-05-02-equity-monitor-design.md` (§17 reserved hooks)
**Goal:** Close the loop from signal → suggested action → user confirm → paper trade execution → P&L tracking, *without* breaking Phase 1.

---

## 1. Scope (MVP-first)

### In scope (P2 MVP — first deliverable)

- `trader/paper.py`: thin wrapper around Futu `OpenSecTradeContext` (paper account)
  with idempotent `place_order`, `query_positions`, `query_today_orders`, `cancel_order`.
- `signals/strategy_lite.py`: rule-based "decision engine" mapping
  (signal_type, severity, indicator state) → `(action, qty)` proposals.
  Hand-written rules; **no ML, no real strategy backtest** — Phase 3 territory.
- DB additions:
  - new `signals.suggested_action` (TEXT: BUY/SELL/HOLD), `signals.suggested_qty` (INTEGER),
    `signals.status` (TEXT: pending/confirmed/cancelled/executed/expired).
  - reuse existing `trades` and `positions` tables (already in Phase-1 schema).
- CLI:
  - `equity-monitor trade list` — show today's pending suggestions.
  - `equity-monitor trade confirm <signal_id> [--qty N]` — manual confirm → paper order.
  - `equity-monitor trade cancel <signal_id>` — drop suggestion.
  - `equity-monitor trade positions` — show open paper positions + unrealized P&L.
  - `equity-monitor trade pnl [--days N]` — historical realized P&L.
- Brief cards (`morning_brief`, `closing_brief`) gain a "Paper P&L" section
  showing open positions, today's fills, and cumulative P&L.
- Hourly job persists `suggested_action` alongside emitted signals.
- `intraday_check` Lark card carries the `signal_id` so users can copy-paste
  it into `trade confirm <id>`.

### Out of scope (deferred to P2 polish or Phase 3)

- Lark Interactive Card action buttons (`[确认买入] [忽略]`) — needs Lark
  card-callback backend; user can confirm via CLI for MVP.
- Lark webhook listener consuming `card_action` events.
- Auto-execution without user confirm — that's Phase 3.
- Risk module (max single position, daily-loss kill-switch, stop-loss/take-profit) — Phase 3.
- Daily review report (`reports/daily_review.py`) — Phase 3.
- Multi-account / margin / options / fractional shares.

---

## 2. Architecture Delta

```
┌──────────────────────────────┐
│  Phase 1 (unchanged)         │
│   intraday_check job         │
│   ├─ snapshot/kline          │
│   ├─ compute indicators      │
│   ├─ detect_threshold        │
│   ├─ detect_tech             │
│   └─ emit Signal             │
└──────────────┬───────────────┘
               │ (NEW IN P2)
               ▼
   ┌────────────────────────┐
   │ strategy_lite          │   ← rule-based, deterministic
   │ Signal → SignalSuggest │
   │ (action, qty)          │
   └─────────┬──────────────┘
             ▼
   ┌────────────────────────┐
   │ persist signal+suggest │
   │ status=pending         │
   └─────────┬──────────────┘
             ▼
   ┌────────────────────────┐
   │ render_signal_alert v2 │   ← appends "Suggested: BUY 100 @ 175.5
   │ + signal_id            │     confirm: equity-monitor trade confirm 42"
   └─────────┬──────────────┘
             ▼ Lark IM
       ┌─────┴─────┐
       │ User reads card                            │
       │ runs `equity-monitor trade confirm 42`     │
       └─────┬─────┘
             ▼
   ┌────────────────────────┐
   │ trader.paper           │   ← OpenSecTradeContext
   │ place_order(...)       │
   └─────────┬──────────────┘
             ▼
   ┌────────────────────────┐
   │ DB: trades + positions │
   │ signal.status=executed │
   └────────────────────────┘
```

`closing_brief` and `morning_brief` query `trades` + `positions` to render the
P&L section.

---

## 3. New / Modified Tables

### 3.1 `signals` (modified — additive columns only)

```sql
ALTER TABLE signals ADD COLUMN suggested_action TEXT;       -- BUY | SELL | HOLD | NULL
ALTER TABLE signals ADD COLUMN suggested_qty INTEGER;       -- positive int or NULL
ALTER TABLE signals ADD COLUMN status TEXT NOT NULL DEFAULT 'pending';
                                       -- pending | confirmed | executed | cancelled | expired
ALTER TABLE signals ADD COLUMN executed_trade_id INTEGER;   -- FK→trades.id, set on success
```

Backwards-compatible: existing `signals` rows treat `status='pending'` as a
no-op (CLI filters by `suggested_action IS NOT NULL`).

### 3.2 `trades` (already exists, used as-is)

Phase-1 schema preview already declares this table. P2 will write to it for
the first time.

### 3.3 `positions` (already exists, used as-is)

Recomputed on every fill: `qty`, `avg_cost`, `realized_pnl`. `unrealized_pnl`
is recomputed at brief-time using latest snapshot price (not stored
incrementally).

---

## 4. New Modules

```
src/equity_monitor/
├── trader/
│   ├── __init__.py
│   ├── paper.py          # PaperTrader Protocol + OpenDSecTrader + FakePaperTrader
│   └── pnl.py            # compute_realized_pnl, compute_unrealized_pnl, summarize_today
└── signals/
    └── strategy_lite.py  # decide_action(signal, indicator_row) → SignalSuggest
```

### 4.1 `trader/paper.py`

```python
class PaperTrader(Protocol):
    def place_order(
        self,
        *,
        code: str,
        side: Literal["BUY", "SELL"],
        qty: int,
        order_type: Literal["MARKET", "LIMIT"] = "MARKET",
        limit_price: float | None = None,
    ) -> PaperOrderResult: ...
    def cancel_order(self, order_id: str) -> None: ...
    def query_positions(self) -> list[PaperPosition]: ...
    def query_today_orders(self) -> list[PaperOrder]: ...

@dataclass
class PaperOrderResult:
    order_id: str
    status: Literal["FILLED", "PENDING", "REJECTED"]
    filled_qty: int
    avg_fill_price: float
    error: str | None = None
```

`OpenDSecTrader` wraps `futu.OpenSecTradeContext` with `unlock_trade`,
`place_order`, `position_list_query`, `order_list_query`. Tenacity retries.

`FakePaperTrader` is in-memory; tests inject it via the same protocol.

### 4.2 `signals/strategy_lite.py`

Hand-coded rules (deterministic, testable):

| Signal | Severity | Decision |
|---|---|---|
| `threshold_breach_lower` | CRITICAL | BUY 100 (price dipped below user-set support) |
| `threshold_breach_upper` | CRITICAL | SELL all open qty (price hit user-set resistance) |
| `rsi_oversold` + `macd_golden_cross` (same code, same hour) | WARN+INFO | BUY 50 |
| `rsi_overbought` + `macd_death_cross` | WARN | SELL 50 |
| `boll_lower_break` + `rsi < 30` | INFO | HOLD (suggest only, no qty) |
| anything else | * | none (no suggestion emitted) |

Position-aware: SELL is capped by current open qty; BUY is rejected if
existing qty already ≥ `max_position_per_symbol` (default 200, configurable).

Returns `SignalSuggest(action, qty, reason: str)` or `None`.

### 4.3 `trader/pnl.py`

Pure functions, no DB calls — operates on in-memory `Trade` and `Position`
sequences.

- `apply_fill(positions: dict[str, Position], trade: Trade) → tuple[Position, float]`:
  weighted-average cost on BUY; FIFO realized P&L on SELL.
- `unrealized_pnl(position, mark_price) → float`
- `summarize_today(trades, positions, snapshots) → BriefPnLSummary`

---

## 5. New CLI Commands

```
equity-monitor trade list [--status pending|confirmed|all]
                                    Show today's suggestions with signal_id.
equity-monitor trade confirm <signal_id> [--qty N]
                                    Place paper order for the suggestion.
                                    --qty overrides suggested_qty.
equity-monitor trade cancel <signal_id>
                                    Mark suggestion as cancelled.
equity-monitor trade positions      Open paper positions + mark-to-market P&L.
equity-monitor trade pnl [--days N] Realized P&L for last N days (default 7).
```

`trade confirm` flow:

1. Load signal row by id; require `status='pending'` and `suggested_action` set.
2. Open `OpenDSecTrader`, place order.
3. On success, write a `trades` row, update `positions`, set
   `signal.status='executed'`, `signal.executed_trade_id=<id>`.
4. On rejection, set `signal.status='cancelled'`, log error, exit 1.

---

## 6. Brief Card P&L Section

Existing `daily_brief.json.j2` template gains an optional "Paper P&L"
section, rendered only if there's any open position or any trade today:

```
─── Paper P&L (today) ───
US.AAPL  +50 @ 178.30  (1 fill)  unrealized +$54
US.NVDA  +20 @ 142.10  (1 fill)  unrealized -$8
─── Cumulative ───
realized:    +$246  (last 30 days)
open value:  $9,712
exposure:    32% of cash
```

Pure additive — Phase-1 brief still renders correctly when no trades exist.

---

## 7. Risk Notes (P2 MVP)

- Paper account only. Phase 2 must NEVER touch real money even if the
  Futu trade context is logged into a real account by mistake. Defensive
  check: `OpenDSecTrader.__init__` queries `acc_list_query`, picks the
  paper account by `trd_env=SIMULATE`, raises if absent.
- All quantities are bounded by `signals.max_position_per_symbol` config
  (default 200) and `signals.max_concurrent_positions` (default 5).
- `trade confirm` is idempotent on `signal_id`: re-running on already
  executed signal returns the existing trade_id without placing a new order.
- After 4 trading-day-EOD without confirm, suggestions auto-expire
  (`status='expired'`). Daily cron job; not in MVP — added to P2 polish.

---

## 8. Test Plan (TDD-first)

| Layer | Tests |
|---|---|
| `trader/paper.py` | FakePaperTrader full ledger; OpenDSecTrader unit-mocked subprocess return shapes |
| `signals/strategy_lite.py` | Decision matrix table-driven test; position-aware caps |
| `trader/pnl.py` | apply_fill BUY/SELL avg-cost; realized FIFO; mixed series |
| CLI `trade confirm/cancel/list` | click.testing.CliRunner with mocked PaperTrader |
| Integration: `intraday_check` w/ strategy_lite | signal+suggest pipeline end-to-end vs FakeFutuClient |

---

## 9. Acceptance (P2 MVP)

- [ ] `equity-monitor trade list` shows pending suggestions correctly.
- [ ] `equity-monitor trade confirm <id>` writes a trade row + updates positions.
- [ ] `equity-monitor trade positions` shows open positions w/ live mark price.
- [ ] `closing_brief` Lark card includes Paper P&L section when trades exist.
- [ ] All Phase-1 tests still pass.
- [ ] New 25+ tests cover P2 modules.

---

## 10. Migration Plan

1. Add 4 columns to `signals` table (one Alembic revision, all nullable except
   `status DEFAULT 'pending'`).
2. No data migration; existing rows keep NULL `suggested_action`.
3. Deployment: stop runner, `equity-monitor db migrate`, restart runner.

---

**End of Phase 2 Spec MVP.**
