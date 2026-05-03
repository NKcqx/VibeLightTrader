"""Integration: run_intraday_check auto-executes BUY/SELL suggestions
when paper_trader is injected AND cfg.trader.auto_execute is True.

Three paths covered:
  1. happy: actionable suggestion → Trade row + Position row written.
  2. switch off: cfg.trader.auto_execute=False → suggestion shown, no trade.
  3. error isolation: broker REJECTED → warning logged, no trade, no crash.
  4. idempotency: re-running same intraday_check does not double-trade.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import sessionmaker

from equity_monitor.config import AppConfig, WatchlistConfig
from equity_monitor.db import session_scope
from equity_monitor.futu_client import Candle, FakeFutuClient, Snapshot
from equity_monitor.models import Position, Symbol, Trade
from equity_monitor.scheduler.jobs import run_intraday_check
from equity_monitor.trader.paper import FakePaperTrader


def _make_flat_candles(start_ts: datetime, n: int, close: float) -> list[Candle]:
    """Constant-price kline so technical signals stay quiet; only threshold fires."""
    return [
        Candle(
            code="US.AAPL",
            ts=start_ts + timedelta(hours=h),
            open=close,
            high=close + 0.5,
            low=close - 0.5,
            close=close,
            volume=10_000,
            turnover=close * 10_000,
        )
        for h in range(n)
    ]


def _seed_apple(factory: sessionmaker) -> None:
    with session_scope(factory) as s:
        s.add(
            Symbol(
                code="US.AAPL",
                name="Apple",
                upper_threshold=200.0,
                lower_threshold=165.0,
            )
        )


def _set_market(fake_futu: FakeFutuClient, last_price: float, base_ts: datetime) -> None:
    fake_futu.set_kline(
        "US.AAPL", "K_60M", _make_flat_candles(base_ts, 40, close=last_price)
    )
    fake_futu.set_snapshot(
        Snapshot(
            code="US.AAPL",
            last_price=last_price,
            open_price=last_price,
            high_price=last_price + 0.5,
            low_price=last_price - 0.5,
            volume=12_000_000,
            turnover=last_price * 12_000_000,
            update_time=base_ts + timedelta(hours=39),
        )
    )


def _silent_sender(card, open_id, receiver_type):  # type: ignore[no-untyped-def]
    return "om_test"


@pytest.mark.integration
def test_lower_threshold_breach_auto_executes_buy(
    factory: sessionmaker,
    fake_futu: FakeFutuClient,
    app_cfg: AppConfig,
    watchlist: WatchlistConfig,
) -> None:
    _seed_apple(factory)
    base_ts = datetime(2026, 5, 4, 9, 30)
    _set_market(fake_futu, last_price=160.0, base_ts=base_ts)  # < lower 165
    trader = FakePaperTrader(mark_price={"US.AAPL": 160.0})

    out = run_intraday_check(
        client=fake_futu,
        factory=factory,
        cfg=app_cfg,
        watchlist=watchlist,
        send_card_fn=_silent_sender,
        paper_trader=trader,
    )

    assert out["suggestions"] >= 1, "lower threshold should produce a BUY suggestion"
    assert out["executed"] == 1, f"expected 1 auto-trade, got {out!r}"

    with session_scope(factory) as s:
        trades = s.query(Trade).all()
        positions = s.query(Position).filter(Position.qty > 0).all()
        assert len(trades) == 1, f"expected 1 Trade row, got {trades!r}"
        assert trades[0].side == "BUY"
        assert trades[0].qty == 100  # critical_size from strategy_lite
        assert trades[0].price == pytest.approx(160.0)
        assert trades[0].signal_id is not None
        assert len(positions) == 1
        assert positions[0].qty == 100
        assert positions[0].avg_cost == pytest.approx(160.0)


@pytest.mark.integration
def test_auto_execute_disabled_no_trade_persisted(
    factory: sessionmaker,
    fake_futu: FakeFutuClient,
    app_cfg: AppConfig,
    watchlist: WatchlistConfig,
) -> None:
    _seed_apple(factory)
    base_ts = datetime(2026, 5, 4, 9, 30)
    _set_market(fake_futu, last_price=160.0, base_ts=base_ts)
    trader = FakePaperTrader(mark_price={"US.AAPL": 160.0})

    app_cfg.trader.auto_execute = False  # explicit OFF

    out = run_intraday_check(
        client=fake_futu,
        factory=factory,
        cfg=app_cfg,
        watchlist=watchlist,
        send_card_fn=_silent_sender,
        paper_trader=trader,
    )

    assert out["suggestions"] >= 1, "BUY suggestion should still be produced"
    assert out["executed"] == 0, "auto_execute=False must skip all trades"

    with session_scope(factory) as s:
        assert s.query(Trade).count() == 0
        assert s.query(Position).filter(Position.qty > 0).count() == 0


@pytest.mark.integration
def test_no_paper_trader_no_trade_persisted(
    factory: sessionmaker,
    fake_futu: FakeFutuClient,
    app_cfg: AppConfig,
    watchlist: WatchlistConfig,
) -> None:
    _seed_apple(factory)
    base_ts = datetime(2026, 5, 4, 9, 30)
    _set_market(fake_futu, last_price=160.0, base_ts=base_ts)

    out = run_intraday_check(
        client=fake_futu,
        factory=factory,
        cfg=app_cfg,
        watchlist=watchlist,
        send_card_fn=_silent_sender,
        paper_trader=None,  # explicitly omit
    )

    assert out["suggestions"] >= 1
    assert out["executed"] == 0
    with session_scope(factory) as s:
        assert s.query(Trade).count() == 0


@pytest.mark.integration
def test_broker_rejection_isolated_does_not_crash(
    factory: sessionmaker,
    fake_futu: FakeFutuClient,
    app_cfg: AppConfig,
    watchlist: WatchlistConfig,
) -> None:
    _seed_apple(factory)
    base_ts = datetime(2026, 5, 4, 9, 30)
    _set_market(fake_futu, last_price=160.0, base_ts=base_ts)
    # Broker has NO mark price for US.AAPL → REJECTED
    trader = FakePaperTrader(mark_price={})

    out = run_intraday_check(
        client=fake_futu,
        factory=factory,
        cfg=app_cfg,
        watchlist=watchlist,
        send_card_fn=_silent_sender,
        paper_trader=trader,
    )

    assert out["suggestions"] >= 1
    assert out["executed"] == 0, "rejection must not count as executed"
    with session_scope(factory) as s:
        assert s.query(Trade).count() == 0
        assert s.query(Position).filter(Position.qty > 0).count() == 0


@pytest.mark.integration
def test_idempotent_repeat_run_does_not_double_trade(
    factory: sessionmaker,
    fake_futu: FakeFutuClient,
    app_cfg: AppConfig,
    watchlist: WatchlistConfig,
) -> None:
    _seed_apple(factory)
    base_ts = datetime(2026, 5, 4, 9, 30)
    _set_market(fake_futu, last_price=160.0, base_ts=base_ts)
    trader = FakePaperTrader(mark_price={"US.AAPL": 160.0})

    out1 = run_intraday_check(
        client=fake_futu,
        factory=factory,
        cfg=app_cfg,
        watchlist=watchlist,
        send_card_fn=_silent_sender,
        paper_trader=trader,
    )
    out2 = run_intraday_check(
        client=fake_futu,
        factory=factory,
        cfg=app_cfg,
        watchlist=watchlist,
        send_card_fn=_silent_sender,
        paper_trader=trader,
    )

    assert out1["executed"] == 1
    assert out2["executed"] == 0, (
        "second run on identical signal should ON CONFLICT DO NOTHING and "
        "thus skip auto-execution"
    )
    with session_scope(factory) as s:
        assert s.query(Trade).count() == 1
