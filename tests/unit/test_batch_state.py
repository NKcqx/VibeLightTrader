"""Step 3 — position-cycle batch tracking.

Covers:
  * ``_load_batch_state``: SQL-driven reconstruction of batch_index +
    days_since_last_buy across the typical cycle shapes (no trades,
    open cycle, fully closed, re-opened after exit).
  * ``enforce_constraints`` batch / cooldown demotions to HOLD.
  * Prompt rendering surfaces the cycle state and HARD STOP / COOLDOWN
    callouts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from vibe_trader.db import init_schema, make_engine, make_sessionmaker, session_scope
from vibe_trader.llm.prompt import (
    DEFAULT_USER_TEMPLATE,
    ParsedDecision,
    render_user_prompt,
)
from vibe_trader.models import Symbol, Trade
from vibe_trader.scheduler.jobs import _load_batch_state
from vibe_trader.signals.strategy_llm import (
    ConstraintViolation,
    enforce_constraints,
)


# ---------------------------------------------------------------------------
# enforce_constraints batch / cooldown rules.
# ---------------------------------------------------------------------------


def _buy(qty: int = 10, conf: float = 0.8) -> ParsedDecision:
    return ParsedDecision(action="BUY", qty=qty, confidence=conf, reason="ok")


def test_enforce_buy_demoted_to_hold_when_at_max_batches() -> None:
    out = enforce_constraints(
        _buy(),
        position_qty=20,
        max_position=200,
        min_trade_size=10,
        min_confidence=0.6,
        batch_index=3,
        max_batches=3,
    )
    assert out.action == "HOLD"
    assert "已达最大批次" in out.reason
    assert "llm_batch_capped" in out.triggering_signal_types


def test_enforce_buy_demoted_to_hold_when_in_cooldown() -> None:
    out = enforce_constraints(
        _buy(),
        position_qty=20,
        max_position=200,
        min_trade_size=10,
        min_confidence=0.6,
        batch_index=1,
        days_since_last_buy=2,
        max_batches=3,
        add_cooldown_days=5,
    )
    assert out.action == "HOLD"
    assert "冷却" in out.reason
    assert "还需 3d" in out.reason
    assert "llm_cooldown" in out.triggering_signal_types


def test_enforce_buy_passes_when_cooldown_satisfied() -> None:
    out = enforce_constraints(
        _buy(),
        position_qty=20,
        max_position=200,
        min_trade_size=10,
        min_confidence=0.6,
        batch_index=1,
        days_since_last_buy=10,
        max_batches=3,
        add_cooldown_days=5,
    )
    assert out.action == "BUY"


def test_enforce_buy_passes_when_no_position_yet() -> None:
    """No current cycle (batch_index=0, days_since_last_buy=None)."""
    out = enforce_constraints(
        _buy(),
        position_qty=0,
        max_position=200,
        min_trade_size=10,
        min_confidence=0.6,
        batch_index=0,
        days_since_last_buy=None,
        max_batches=3,
        add_cooldown_days=5,
    )
    assert out.action == "BUY"


def test_enforce_sell_unaffected_by_batch_caps() -> None:
    sell = ParsedDecision(action="SELL", qty=10, confidence=0.8, reason="ok")
    out = enforce_constraints(
        sell,
        position_qty=20,
        max_position=200,
        min_trade_size=10,
        min_confidence=0.6,
        batch_index=3,
        days_since_last_buy=1,
        max_batches=3,
        add_cooldown_days=5,
    )
    assert out.action == "SELL"


def test_enforce_max_position_still_raises() -> None:
    """Position-overshoot is a malformed-output bug, not a policy demotion."""
    with pytest.raises(ConstraintViolation):
        enforce_constraints(
            _buy(qty=200),
            position_qty=50,
            max_position=200,
            min_trade_size=10,
            min_confidence=0.6,
            batch_index=0,
            max_batches=3,
        )


# ---------------------------------------------------------------------------
# Prompt rendering: cycle block + HARD STOP / COOLDOWN callouts.
# ---------------------------------------------------------------------------


@dataclass
class _Profile:
    enabled: bool = True
    horizon_months_min: int = 3
    horizon_months_max: int = 6
    style: str = "growth"
    theme: str = "Test"
    budget_per_symbol_usd: float = 50_000
    drawdown_tolerance_pct: float = 20
    max_concentration_pct: float = 60
    initial_entry_pct: float = 40
    max_batches: int = 3
    add_on_dip_pct: float = 5
    add_cooldown_days: int = 5
    prefer_dip_buy: bool = True
    take_profit_pct: float = 30
    take_profit_trim_pct: float = 50
    hard_stop_pct: float = 20
    min_holding_days: int = 30


def _render(**kw):
    base = dict(
        code="US.NVDA",
        snapshot=None,
        position_qty=20,
        avg_cost=100.0,
        realized_pnl=0.0,
        intraday_return=None,
        last_30_bar_return=None,
        indicators=None,
        signals=[],
        max_position=200,
        min_trade_size=10,
        min_confidence=0.6,
    )
    base.update(kw)
    return render_user_prompt(**base)


def test_prompt_contains_cycle_block_with_max_batches() -> None:
    out = _render(profile=_Profile(), batch_index=2, days_since_last_buy=10)
    assert "Position cycle:" in out
    assert "batch_index:" in out
    assert "max_batches=3" in out
    assert "add_cooldown_days=5" in out
    assert "days_since_last_buy:   10" in out


def test_prompt_hard_stop_callout_when_at_cap() -> None:
    out = _render(profile=_Profile(), batch_index=3, days_since_last_buy=20)
    assert "HARD STOP" in out
    assert "BUY is forbidden" in out


def test_prompt_cooldown_callout_when_inside_window() -> None:
    out = _render(profile=_Profile(), batch_index=1, days_since_last_buy=2)
    assert "COOLDOWN" in out
    assert "still 3 day" in out


def test_prompt_omits_cycle_block_when_no_batch_passed() -> None:
    out = _render(profile=_Profile())  # batch_index defaults to None
    assert "Position cycle:" not in out


# ---------------------------------------------------------------------------
# _load_batch_state — SQL-driven scenarios.
# ---------------------------------------------------------------------------


@pytest.fixture
def factory(tmp_path: Path):
    db_path = tmp_path / "trades.db"
    engine = make_engine(str(db_path), wal_mode=False)
    init_schema(engine)
    yield make_sessionmaker(engine)
    engine.dispose()


def _add_symbol(session, code: str = "US.NVDA") -> int:
    sym = Symbol(code=code, name=code.split(".", 1)[1])
    session.add(sym)
    session.flush()
    return sym.id


def _add_trade(
    session,
    symbol_id: int,
    side: str,
    qty: int,
    ts: datetime,
    *,
    status: str = "FILLED",
    price: float = 100.0,
) -> None:
    session.add(
        Trade(
            symbol_id=symbol_id,
            ts=ts,
            side=side,
            qty=qty,
            price=price,
            status=status,
        )
    )


def test_batch_state_no_trades(factory) -> None:
    with session_scope(factory) as session:
        _add_symbol(session)
    with session_scope(factory) as session:
        out = _load_batch_state(session, today=date(2026, 5, 7))
    assert out == {}


def test_batch_state_single_buy_open(factory) -> None:
    with session_scope(factory) as session:
        sid = _add_symbol(session)
        _add_trade(session, sid, "BUY", 10, datetime(2026, 5, 1, 14, tzinfo=timezone.utc))
    with session_scope(factory) as session:
        out = _load_batch_state(session, today=date(2026, 5, 7))
    assert out["US.NVDA"] == (1, 6)


def test_batch_state_two_buys_open(factory) -> None:
    with session_scope(factory) as session:
        sid = _add_symbol(session)
        _add_trade(session, sid, "BUY", 10, datetime(2026, 5, 1, 14, tzinfo=timezone.utc))
        _add_trade(session, sid, "BUY", 10, datetime(2026, 5, 5, 14, tzinfo=timezone.utc))
    with session_scope(factory) as session:
        out = _load_batch_state(session, today=date(2026, 5, 7))
    assert out["US.NVDA"] == (2, 2)


def test_batch_state_closed_position_returns_no_entry(factory) -> None:
    with session_scope(factory) as session:
        sid = _add_symbol(session)
        _add_trade(session, sid, "BUY", 10, datetime(2026, 5, 1, 14, tzinfo=timezone.utc))
        _add_trade(session, sid, "SELL", 10, datetime(2026, 5, 3, 14, tzinfo=timezone.utc))
    with session_scope(factory) as session:
        out = _load_batch_state(session, today=date(2026, 5, 7))
    assert "US.NVDA" not in out


def test_batch_state_cycle_reopens_after_exit(factory) -> None:
    """SELL that drains qty resets the cycle; subsequent BUY = batch 1, not 2."""
    with session_scope(factory) as session:
        sid = _add_symbol(session)
        _add_trade(session, sid, "BUY", 10, datetime(2026, 4, 1, 14, tzinfo=timezone.utc))
        _add_trade(session, sid, "SELL", 10, datetime(2026, 4, 15, 14, tzinfo=timezone.utc))
        _add_trade(session, sid, "BUY", 5, datetime(2026, 5, 4, 14, tzinfo=timezone.utc))
    with session_scope(factory) as session:
        out = _load_batch_state(session, today=date(2026, 5, 7))
    assert out["US.NVDA"] == (1, 3)


def test_batch_state_partial_sell_keeps_cycle(factory) -> None:
    """Selling some-but-not-all shares does NOT reset the cycle."""
    with session_scope(factory) as session:
        sid = _add_symbol(session)
        _add_trade(session, sid, "BUY", 10, datetime(2026, 5, 1, 14, tzinfo=timezone.utc))
        _add_trade(session, sid, "BUY", 10, datetime(2026, 5, 2, 14, tzinfo=timezone.utc))
        _add_trade(session, sid, "SELL", 5, datetime(2026, 5, 3, 14, tzinfo=timezone.utc))
    with session_scope(factory) as session:
        out = _load_batch_state(session, today=date(2026, 5, 7))
    assert out["US.NVDA"] == (2, 5)  # 2 BUYs in cycle, last buy 5d ago


def test_batch_state_ignores_pending_orders(factory) -> None:
    with session_scope(factory) as session:
        sid = _add_symbol(session)
        _add_trade(session, sid, "BUY", 10, datetime(2026, 5, 1, 14, tzinfo=timezone.utc))
        _add_trade(
            session, sid, "BUY", 10,
            datetime(2026, 5, 5, 14, tzinfo=timezone.utc),
            status="PENDING",
        )
    with session_scope(factory) as session:
        out = _load_batch_state(session, today=date(2026, 5, 7))
    assert out["US.NVDA"] == (1, 6)  # only the FILLED row counts


def test_batch_state_today_none_skips_days_since(factory) -> None:
    with session_scope(factory) as session:
        sid = _add_symbol(session)
        _add_trade(session, sid, "BUY", 10, datetime(2026, 5, 1, 14, tzinfo=timezone.utc))
    with session_scope(factory) as session:
        out = _load_batch_state(session, today=None)
    assert out["US.NVDA"] == (1, None)
