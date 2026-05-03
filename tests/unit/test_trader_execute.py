"""Unit tests for trader/execute.py:execute_signal_trade.

Covers the three broker outcomes (FILLED / PENDING / REJECTED) that the
production OpenDSecTrader can return; FakePaperTrader only models FILLED
+ REJECTED, so PENDING is exercised through a hand-rolled stub.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from equity_monitor.db import init_schema, make_engine, make_sessionmaker, session_scope
from equity_monitor.models import Position, Signal as SignalRow, Symbol, Trade
from equity_monitor.trader.execute import SignalExecutionError, execute_signal_trade
from equity_monitor.trader.paper import PaperOrderResult


@pytest.fixture
def engine(tmp_path) -> Engine:
    db = tmp_path / "test.db"
    eng = make_engine(db, wal_mode=False)
    init_schema(eng)
    return eng


@pytest.fixture
def factory(engine: Engine) -> sessionmaker[Session]:
    return make_sessionmaker(engine)


def _seed_signal(factory, action: str, qty: int) -> tuple[int, int]:
    """Insert a Symbol + a pending Signal with the given suggestion;
    return (symbol_id, signal_id).
    """
    with session_scope(factory) as s:
        sym = Symbol(code="US.AAPL", name="Apple")
        s.add(sym)
        s.flush()
        sig = SignalRow(
            symbol_id=sym.id,
            ts=datetime(2026, 5, 4, 13, 30, tzinfo=timezone.utc),
            signal_type="threshold_breach_lower",
            severity="CRITICAL",
            payload_json="{}",
            delivered=False,
            suggested_action=action,
            suggested_qty=qty,
            status="pending",
        )
        s.add(sig)
        s.flush()
        return sym.id, sig.id


@dataclass
class _StubTrader:
    """Returns whatever PaperOrderResult was injected, regardless of args."""

    result: PaperOrderResult

    def place_order(self, **kw) -> PaperOrderResult:
        return self.result

    def close(self) -> None:
        pass


def _result(status: str, *, qty: int, price: float) -> PaperOrderResult:
    return PaperOrderResult(
        order_id="oid_test",
        status=status,  # type: ignore[arg-type]
        code="US.AAPL",
        side="BUY",
        requested_qty=qty,
        filled_qty=qty if status == "FILLED" else 0,
        avg_fill_price=price if status == "FILLED" else 0.0,
        submitted_at=datetime(2026, 5, 4, 13, 30, tzinfo=timezone.utc),
    )


def test_pending_order_records_trade_but_skips_position_mutation(factory) -> None:
    """After-hours BUY → broker queues PENDING → Trade row written, but
    Position must NOT be inflated with avg_cost=0 (the bug we hit on 2026-05-03)."""
    sym_id, sig_id = _seed_signal(factory, action="BUY", qty=100)
    trader = _StubTrader(_result("PENDING", qty=100, price=0.0))

    with session_scope(factory) as s:
        sig = s.query(SignalRow).filter(SignalRow.id == sig_id).one()
        sym = s.query(Symbol).filter(Symbol.id == sym_id).one()
        trade_id = execute_signal_trade(s, sig, sym, 100, trader)

    with session_scope(factory) as s:
        trade = s.query(Trade).filter(Trade.id == trade_id).one()
        assert trade.status == "PENDING"
        assert trade.qty == 100, "Trade row should record the *requested* qty"
        assert trade.price == 0.0, "PENDING price is 0 until fill"
        assert trade.signal_id == sig_id

        positions = s.query(Position).filter(Position.qty > 0).all()
        assert positions == [], (
            "PENDING orders must NOT update Position; this protects against "
            "after-hours queue inflating qty with avg_cost=0"
        )

        sig = s.query(SignalRow).filter(SignalRow.id == sig_id).one()
        assert sig.status == "executed"
        assert sig.executed_trade_id == trade_id


def test_filled_order_creates_position(factory) -> None:
    sym_id, sig_id = _seed_signal(factory, action="BUY", qty=50)
    trader = _StubTrader(_result("FILLED", qty=50, price=180.25))

    with session_scope(factory) as s:
        sig = s.query(SignalRow).filter(SignalRow.id == sig_id).one()
        sym = s.query(Symbol).filter(Symbol.id == sym_id).one()
        execute_signal_trade(s, sig, sym, 50, trader)

    with session_scope(factory) as s:
        pos = s.query(Position).filter(Position.symbol_id == sym_id).one()
        assert pos.qty == 50
        assert pos.avg_cost == pytest.approx(180.25)


def test_rejected_order_cancels_signal_and_raises(factory) -> None:
    sym_id, sig_id = _seed_signal(factory, action="BUY", qty=10)
    trader = _StubTrader(
        PaperOrderResult(
            order_id="",
            status="REJECTED",
            code="US.AAPL",
            side="BUY",
            requested_qty=10,
            filled_qty=0,
            avg_fill_price=0.0,
            submitted_at=datetime(2026, 5, 4, 13, 30, tzinfo=timezone.utc),
            error="no mark price",
        )
    )

    with session_scope(factory) as s:
        sig = s.query(SignalRow).filter(SignalRow.id == sig_id).one()
        sym = s.query(Symbol).filter(Symbol.id == sym_id).one()
        with pytest.raises(SignalExecutionError, match="rejected by paper broker"):
            execute_signal_trade(s, sig, sym, 10, trader)

    with session_scope(factory) as s:
        sig = s.query(SignalRow).filter(SignalRow.id == sig_id).one()
        assert sig.status == "cancelled"
        assert s.query(Trade).count() == 0
        assert s.query(Position).filter(Position.qty > 0).count() == 0
