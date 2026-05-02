from __future__ import annotations

import pytest

from equity_monitor.trader.paper import (
    FakePaperTrader,
    PaperTradeError,
)


def test_market_buy_fills_at_mark_and_creates_position() -> None:
    t = FakePaperTrader()
    t.set_mark("US.AAPL", 180.0)

    out = t.place_order(code="US.AAPL", side="BUY", qty=10)
    assert out.status == "FILLED"
    assert out.filled_qty == 10
    assert out.avg_fill_price == 180.0

    pos = t.query_positions()
    assert len(pos) == 1
    assert pos[0].qty == 10
    assert pos[0].avg_cost == 180.0
    assert pos[0].market_value == 1800.0
    assert pos[0].unrealized_pnl == 0.0


def test_buy_uses_weighted_average_cost() -> None:
    t = FakePaperTrader()
    t.set_mark("US.AAPL", 100.0)
    t.place_order(code="US.AAPL", side="BUY", qty=10)
    t.set_mark("US.AAPL", 120.0)
    t.place_order(code="US.AAPL", side="BUY", qty=10)

    pos = t.query_positions()[0]
    assert pos.qty == 20
    assert pos.avg_cost == 110.0  # (10*100 + 10*120) / 20
    assert pos.unrealized_pnl == (120.0 - 110.0) * 20  # +200


def test_sell_reduces_qty_and_keeps_avg_cost() -> None:
    t = FakePaperTrader()
    t.set_mark("US.AAPL", 100.0)
    t.place_order(code="US.AAPL", side="BUY", qty=10)
    t.set_mark("US.AAPL", 130.0)
    t.place_order(code="US.AAPL", side="SELL", qty=4)

    pos = t.query_positions()[0]
    assert pos.qty == 6
    assert pos.avg_cost == 100.0  # avg cost of remaining unchanged


def test_sell_full_position_clears_it() -> None:
    t = FakePaperTrader()
    t.set_mark("US.AAPL", 100.0)
    t.place_order(code="US.AAPL", side="BUY", qty=5)
    t.place_order(code="US.AAPL", side="SELL", qty=5)
    assert t.query_positions() == []


def test_sell_more_than_held_rejected() -> None:
    t = FakePaperTrader()
    t.set_mark("US.AAPL", 100.0)
    t.place_order(code="US.AAPL", side="BUY", qty=3)

    out = t.place_order(code="US.AAPL", side="SELL", qty=10)
    assert out.status == "REJECTED"
    assert out.filled_qty == 0
    assert "insufficient" in (out.error or "")
    assert t.query_positions()[0].qty == 3  # untouched


def test_market_order_without_mark_rejected() -> None:
    t = FakePaperTrader()
    out = t.place_order(code="US.UNKNOWN", side="BUY", qty=10)
    assert out.status == "REJECTED"
    assert "no mark price" in (out.error or "")


def test_qty_zero_or_negative_rejected() -> None:
    t = FakePaperTrader()
    t.set_mark("US.AAPL", 100.0)
    out = t.place_order(code="US.AAPL", side="BUY", qty=0)
    assert out.status == "REJECTED"
    out = t.place_order(code="US.AAPL", side="BUY", qty=-1)
    assert out.status == "REJECTED"


def test_limit_order_stays_pending() -> None:
    t = FakePaperTrader()
    out = t.place_order(
        code="US.AAPL", side="BUY", qty=10, order_type="LIMIT", limit_price=170.0
    )
    assert out.status == "PENDING"
    assert out.filled_qty == 0
    assert t.query_positions() == []  # no position until filled

    orders = t.query_today_orders()
    assert len(orders) == 1
    assert orders[0].status == "PENDING"
    assert orders[0].price == 170.0


def test_limit_order_without_price_rejected() -> None:
    t = FakePaperTrader()
    out = t.place_order(code="US.AAPL", side="BUY", qty=10, order_type="LIMIT")
    assert out.status == "REJECTED"
    assert "limit_price" in (out.error or "")


def test_cancel_pending_order_marks_cancelled() -> None:
    t = FakePaperTrader()
    out = t.place_order(
        code="US.AAPL", side="BUY", qty=10, order_type="LIMIT", limit_price=170.0
    )
    t.cancel_order(out.order_id)

    orders = t.query_today_orders()
    assert orders[0].status == "CANCELLED"


def test_cancel_filled_order_raises() -> None:
    t = FakePaperTrader()
    t.set_mark("US.AAPL", 100.0)
    out = t.place_order(code="US.AAPL", side="BUY", qty=10)
    with pytest.raises(PaperTradeError, match="cannot cancel"):
        t.cancel_order(out.order_id)


def test_cancel_unknown_order_raises() -> None:
    t = FakePaperTrader()
    with pytest.raises(PaperTradeError, match="not found"):
        t.cancel_order("po_nonexistent")


def test_query_today_orders_returns_all_in_order() -> None:
    t = FakePaperTrader()
    t.set_mark("US.AAPL", 100.0)
    t.set_mark("US.NVDA", 140.0)

    t.place_order(code="US.AAPL", side="BUY", qty=5)
    t.place_order(code="US.NVDA", side="BUY", qty=10)
    t.place_order(code="US.AAPL", side="SELL", qty=2)

    orders = t.query_today_orders()
    assert [o.code for o in orders] == ["US.AAPL", "US.NVDA", "US.AAPL"]
    assert all(o.status == "FILLED" for o in orders)


def test_close_blocks_further_orders() -> None:
    t = FakePaperTrader()
    t.set_mark("US.AAPL", 100.0)
    t.close()
    assert t.closed is True
    with pytest.raises(PaperTradeError, match="closed"):
        t.place_order(code="US.AAPL", side="BUY", qty=1)
