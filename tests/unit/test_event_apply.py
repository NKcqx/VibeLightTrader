from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from vibe_trader.db import init_schema, make_engine, make_sessionmaker, session_scope
from vibe_trader.events.apply import HELP_TEXT, _avg_cost_from_markers, apply
from vibe_trader.events.grammar import (
    AddCommand,
    HelpCommand,
    ListCommand,
    RemoveCommand,
    ThresholdCommand,
)
from vibe_trader.models import Symbol
from vibe_trader.reports.snapshot import TradeMarker


def _mk(side: str, qty: int, price: float, day: int = 1) -> TradeMarker:
    return TradeMarker(
        ts=datetime(2026, 4, day, tzinfo=timezone.utc),
        side=side,  # type: ignore[arg-type]
        qty=qty,
        price=price,
    )


def test_avg_cost_from_markers_empty_returns_none() -> None:
    assert _avg_cost_from_markers([]) is None


def test_avg_cost_from_markers_single_buy() -> None:
    assert _avg_cost_from_markers([_mk("buy", 10, 100.0)]) == pytest.approx(100.0)


def test_avg_cost_from_markers_two_buys_weighted() -> None:
    # 10 @ 100  +  20 @ 130  →  (1000 + 2600) / 30 = 120
    avg = _avg_cost_from_markers([_mk("buy", 10, 100.0, 1), _mk("buy", 20, 130.0, 2)])
    assert avg == pytest.approx(120.0)


def test_avg_cost_from_markers_partial_sell_keeps_basis() -> None:
    # Buy 10 @ 100, sell 4 @ 150 → remaining 6 @ 100 (sells reduce qty
    # at running avg, not at exit price)
    avg = _avg_cost_from_markers(
        [_mk("buy", 10, 100.0, 1), _mk("sell", 4, 150.0, 2)]
    )
    assert avg == pytest.approx(100.0)


def test_avg_cost_from_markers_full_sell_returns_none() -> None:
    avg = _avg_cost_from_markers(
        [_mk("buy", 10, 100.0, 1), _mk("sell", 10, 150.0, 2)]
    )
    assert avg is None


def test_avg_cost_from_markers_mixed_path() -> None:
    # Buy 10 @ 100  → qty=10, basis=1000
    # Buy 10 @ 200  → qty=20, basis=3000, avg=150
    # Sell 5 @ 250  → qty=15, basis=2250 (5 * 150 removed), avg still 150
    # Buy 5 @ 100   → qty=20, basis=2750, avg=137.5
    avg = _avg_cost_from_markers(
        [
            _mk("buy", 10, 100.0, 1),
            _mk("buy", 10, 200.0, 2),
            _mk("sell", 5, 250.0, 3),
            _mk("buy", 5, 100.0, 4),
        ]
    )
    assert avg == pytest.approx(137.5)


def test_avg_cost_from_markers_skips_zero_price_placeholders() -> None:
    # MARKET orders land in DB as price=0 until reconcile pulls the
    # actual fill back — they must not pollute the running avg.
    avg = _avg_cost_from_markers(
        [_mk("buy", 10, 0.0, 1), _mk("buy", 10, 200.0, 2)]
    )
    assert avg == pytest.approx(200.0)


def test_avg_cost_from_markers_oversell_clamped() -> None:
    # Stale data: a SELL with qty larger than running position. We
    # clamp instead of going negative (we don't model shorts).
    avg = _avg_cost_from_markers(
        [_mk("buy", 5, 100.0, 1), _mk("sell", 10, 150.0, 2)]
    )
    assert avg is None


@pytest.fixture
def factory(tmp_path: Path) -> sessionmaker:
    engine = make_engine(str(tmp_path / "x.db"), wal_mode=False)
    init_schema(engine)
    return make_sessionmaker(engine)


def test_help_returns_text(factory: sessionmaker) -> None:
    out = apply(HelpCommand(), factory)
    assert out == HELP_TEXT
    assert "添加" in out and "删除" in out


def test_list_empty(factory: sessionmaker) -> None:
    out = apply(ListCommand(), factory)
    assert "监控列表为空" in out


def test_add_creates_row(factory: sessionmaker) -> None:
    out = apply(AddCommand(code="US.AAPL", upper=200.0, lower=165.0), factory)
    assert "已添加" in out
    assert "US.AAPL" in out
    assert "200" in out and "165" in out
    with session_scope(factory) as s:
        row = s.query(Symbol).filter(Symbol.code == "US.AAPL").one()
        assert row.upper_threshold == 200.0
        assert row.lower_threshold == 165.0


def test_add_without_thresholds(factory: sessionmaker) -> None:
    out = apply(AddCommand(code="US.TSLA"), factory)
    assert "已添加" in out
    with session_scope(factory) as s:
        row = s.query(Symbol).filter(Symbol.code == "US.TSLA").one()
        assert row.upper_threshold is None
        assert row.lower_threshold is None


def test_add_existing_updates_thresholds(factory: sessionmaker) -> None:
    apply(AddCommand(code="US.AAPL", upper=200.0, lower=165.0), factory)
    out = apply(AddCommand(code="US.AAPL", upper=210.0), factory)
    assert "已更新" in out and "上限→210" in out
    with session_scope(factory) as s:
        row = s.query(Symbol).filter(Symbol.code == "US.AAPL").one()
        assert row.upper_threshold == 210.0
        # lower remains untouched
        assert row.lower_threshold == 165.0


def test_add_existing_no_changes_message(factory: sessionmaker) -> None:
    apply(AddCommand(code="US.AAPL", upper=200.0, lower=165.0), factory)
    out = apply(AddCommand(code="US.AAPL", upper=200.0, lower=165.0), factory)
    assert "已在监控中" in out


def test_list_after_add_shows_rows(factory: sessionmaker) -> None:
    apply(AddCommand(code="US.AAPL", upper=200.0, lower=165.0, name="Apple"), factory)
    apply(AddCommand(code="US.NVDA"), factory)
    out = apply(ListCommand(), factory)
    assert "US.AAPL" in out
    assert "US.NVDA" in out
    assert "Apple" in out
    assert "200" in out and "165" in out


def test_remove_existing(factory: sessionmaker) -> None:
    apply(AddCommand(code="US.AAPL"), factory)
    out = apply(RemoveCommand(code="US.AAPL"), factory)
    assert "已从监控列表移除" in out
    with session_scope(factory) as s:
        assert s.query(Symbol).count() == 0


def test_remove_unknown(factory: sessionmaker) -> None:
    out = apply(RemoveCommand(code="US.AAPL"), factory)
    assert "不在监控列表中" in out


def test_threshold_update(factory: sessionmaker) -> None:
    apply(AddCommand(code="US.AAPL", upper=200.0, lower=165.0), factory)
    out = apply(ThresholdCommand(code="US.AAPL", upper=210.0), factory)
    assert "上限→210" in out
    with session_scope(factory) as s:
        row = s.query(Symbol).filter(Symbol.code == "US.AAPL").one()
        assert row.upper_threshold == 210.0
        assert row.lower_threshold == 165.0


def test_threshold_for_unknown_symbol(factory: sessionmaker) -> None:
    out = apply(ThresholdCommand(code="US.AAPL", upper=210.0), factory)
    assert "不在监控列表中" in out


def test_threshold_no_values_no_op(factory: sessionmaker) -> None:
    apply(AddCommand(code="US.AAPL", upper=200.0, lower=165.0), factory)
    out = apply(ThresholdCommand(code="US.AAPL"), factory)
    assert "未变更" in out
