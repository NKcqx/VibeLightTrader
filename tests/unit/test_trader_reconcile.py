"""Unit tests for trader/reconcile.py:reconcile_pending_fills."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import sessionmaker

from vibe_trader.db import (
    init_schema,
    make_engine,
    make_sessionmaker,
    session_scope,
)
from vibe_trader.models import Symbol, Trade
from vibe_trader.trader.paper import OrderSide, OrderStatus, PaperOrder
from vibe_trader.trader.reconcile import (
    ReconcileResult,
    reconcile_pending_fills,
)


@pytest.fixture
def factory(tmp_path) -> sessionmaker:
    e = make_engine(str(tmp_path / "x.db"), wal_mode=False)
    init_schema(e)
    return make_sessionmaker(e)


@dataclass
class _StubTrader:
    """Minimal PaperTrader: only `query_history_orders` is exercised."""

    orders: list[PaperOrder]
    raise_on_query: bool = False

    def query_history_orders(self, *, start, end=None):  # noqa: ANN001
        if self.raise_on_query:
            raise RuntimeError("network down")
        return list(self.orders)

    # Unused but required to satisfy structural typing if checked
    def place_order(self, **_kw): ...
    def cancel_order(self, _oid): ...
    def query_positions(self): return []
    def query_today_orders(self): return []
    def close(self): ...


def _seed_pending_trade(factory, *, code: str, order_id: str, ts: datetime) -> int:
    with session_scope(factory) as s:
        sym = Symbol(
            code=code, name=code, market="US", currency="USD", lot_size=1,
        )
        s.add(sym)
        s.flush()
        t = Trade(
            symbol_id=sym.id,
            ts=ts,
            side="BUY",
            qty=100,
            price=0.0,
            futu_order_id=order_id,
            status="PENDING",
        )
        s.add(t)
        s.flush()
        return t.id


def _mk_broker_order(
    *, order_id: str, status: OrderStatus, fill_price: float, filled_qty: int
) -> PaperOrder:
    side: OrderSide = "BUY"
    return PaperOrder(
        order_id=order_id,
        code="US.NVDA",
        side=side,
        qty=100,
        price=None,
        status=status,
        submitted_at=datetime.now(tz=timezone.utc),
        filled_qty=filled_qty,
        avg_fill_price=fill_price,
    )


def test_reconcile_no_candidates_returns_zeroes(factory) -> None:
    trader = _StubTrader(orders=[])
    res = reconcile_pending_fills(factory, trader)
    assert res == ReconcileResult(0, 0, 0, 0)


def test_reconcile_writes_back_fill_price(factory) -> None:
    now = datetime.now(tz=timezone.utc)
    tid = _seed_pending_trade(
        factory, code="US.NVDA", order_id="7655914", ts=now - timedelta(days=1)
    )
    trader = _StubTrader(
        orders=[
            _mk_broker_order(
                order_id="7655914", status="FILLED",
                fill_price=198.91, filled_qty=251,
            )
        ]
    )

    res = reconcile_pending_fills(factory, trader, now=now)
    assert res == ReconcileResult(candidates=1, matched=1, updated=1, errors=0)

    with session_scope(factory) as s:
        t = s.get(Trade, tid)
        assert t.price == pytest.approx(198.91)
        assert t.qty == 251
        assert t.status == "FILLED"


def test_reconcile_unmatched_order_id_left_alone(factory) -> None:
    now = datetime.now(tz=timezone.utc)
    tid = _seed_pending_trade(
        factory, code="US.NVDA", order_id="7655914", ts=now - timedelta(days=1)
    )
    trader = _StubTrader(orders=[])  # broker has nothing

    res = reconcile_pending_fills(factory, trader, now=now)
    assert res.candidates == 1 and res.matched == 0 and res.updated == 0

    with session_scope(factory) as s:
        t = s.get(Trade, tid)
        assert t.price == 0.0
        assert t.status == "PENDING"


def test_reconcile_handles_broker_error(factory) -> None:
    now = datetime.now(tz=timezone.utc)
    _seed_pending_trade(
        factory, code="US.NVDA", order_id="x", ts=now - timedelta(days=1)
    )
    trader = _StubTrader(orders=[], raise_on_query=True)

    res = reconcile_pending_fills(factory, trader, now=now)
    assert res.candidates == 1 and res.errors == 1 and res.updated == 0


def test_reconcile_marks_cancelled(factory) -> None:
    now = datetime.now(tz=timezone.utc)
    tid = _seed_pending_trade(
        factory, code="US.NVDA", order_id="abc", ts=now - timedelta(days=1)
    )
    trader = _StubTrader(
        orders=[
            _mk_broker_order(
                order_id="abc", status="CANCELLED",
                fill_price=0.0, filled_qty=0,
            )
        ]
    )

    res = reconcile_pending_fills(factory, trader, now=now)
    assert res.matched == 1
    assert res.updated == 0  # no fill price; just marked cancelled

    with session_scope(factory) as s:
        t = s.get(Trade, tid)
        assert t.status == "CANCELLED"
        assert t.price == 0.0


def test_reconcile_skips_trades_outside_window(factory) -> None:
    now = datetime.now(tz=timezone.utc)
    _seed_pending_trade(
        factory, code="US.NVDA", order_id="old", ts=now - timedelta(days=60)
    )
    trader = _StubTrader(orders=[])

    res = reconcile_pending_fills(factory, trader, now=now)
    assert res.candidates == 0
