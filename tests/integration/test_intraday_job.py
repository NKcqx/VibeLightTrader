from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.orm import sessionmaker

from equity_monitor.config import AppConfig, WatchlistConfig
from equity_monitor.db import session_scope
from equity_monitor.futu_client import Candle, FakeFutuClient, Snapshot
from equity_monitor.models import Indicator
from equity_monitor.models import Signal as SignalRow
from equity_monitor.models import Symbol
from equity_monitor.scheduler.jobs import run_intraday_check


def _make_candles(start_ts: datetime, n: int) -> list[Candle]:
    """Strictly increasing close → produces RSI overbought + MACD positive."""
    return [
        Candle(
            code="US.AAPL",
            ts=start_ts + timedelta(hours=h),
            open=180.0 + h * 0.5,
            high=181.0 + h * 0.5,
            low=179.5 + h * 0.5,
            close=180.5 + h * 0.5,
            volume=10_000,
            turnover=1.8e6,
        )
        for h in range(n)
    ]


@pytest.mark.integration
def test_intraday_check_smoke(
    factory: sessionmaker,
    fake_futu: FakeFutuClient,
    app_cfg: AppConfig,
    watchlist: WatchlistConfig,
) -> None:
    with session_scope(factory) as s:
        s.add(
            Symbol(
                code="US.AAPL",
                name="Apple",
                upper_threshold=200.0,
                lower_threshold=165.0,
            )
        )

    base_ts = datetime(2026, 5, 2, 9, 30)
    fake_futu.set_kline("US.AAPL", "K_60M", _make_candles(base_ts, 40))
    fake_futu.set_snapshot(
        Snapshot(
            code="US.AAPL",
            last_price=199.5,
            open_price=180.0,
            high_price=200.0,
            low_price=179.0,
            volume=12_000_000,
            turnover=2.184e9,
            update_time=base_ts + timedelta(hours=39),
        )
    )

    sent_cards: list[tuple[dict[str, Any], str, str]] = []

    def fake_sender(card, open_id, receiver_type):  # type: ignore[no-untyped-def]
        sent_cards.append((card, open_id, receiver_type))
        return "om_test"

    out = run_intraday_check(
        client=fake_futu,
        factory=factory,
        cfg=app_cfg,
        watchlist=watchlist,
        send_card_fn=fake_sender,
    )

    assert out["quotes"] == 1
    assert out["signals"] >= 0

    with session_scope(factory) as s:
        ind_count = s.query(Indicator).count()
        sig_count = s.query(SignalRow).count()
        assert ind_count == 1
        assert sig_count == out["signals"]

    if out["signals"] > 0:
        assert out["pushed"] >= 1
        assert all(card[1] == "ou_test" for card in sent_cards)


@pytest.mark.integration
def test_intraday_check_threshold_breach_pushes_critical_card(
    factory: sessionmaker,
    fake_futu: FakeFutuClient,
    app_cfg: AppConfig,
    watchlist: WatchlistConfig,
) -> None:
    """Close above upper_threshold (200.0) → CRITICAL signal → 1+ card pushed."""
    with session_scope(factory) as s:
        s.add(
            Symbol(
                code="US.AAPL",
                name="Apple",
                upper_threshold=200.0,
                lower_threshold=165.0,
            )
        )

    base_ts = datetime(2026, 5, 2, 9, 30)
    candles = _make_candles(base_ts, 40)
    candles[-1] = Candle(
        code="US.AAPL",
        ts=candles[-1].ts,
        open=candles[-1].open,
        high=210.0,
        low=candles[-1].low,
        close=205.0,
        volume=20_000,
        turnover=4.1e6,
    )
    fake_futu.set_kline("US.AAPL", "K_60M", candles)
    fake_futu.set_snapshot(
        Snapshot(
            code="US.AAPL",
            last_price=205.0,
            open_price=199.0,
            high_price=210.0,
            low_price=198.0,
            volume=12_000_000,
            turnover=2.5e9,
            update_time=base_ts + timedelta(hours=39),
        )
    )

    sent: list[tuple[dict[str, Any], str, str]] = []

    def sender(card, open_id, receiver_type):  # type: ignore[no-untyped-def]
        sent.append((card, open_id, receiver_type))
        return "om_x"

    out = run_intraday_check(
        client=fake_futu,
        factory=factory,
        cfg=app_cfg,
        watchlist=watchlist,
        send_card_fn=sender,
    )

    sig_types_pushed = [
        c[0]["header"]["title"]["content"] for c in sent if c
    ]
    assert out["pushed"] >= 1, "expected at least one card pushed for CRITICAL"
    assert any("US.AAPL" in title for title in sig_types_pushed)


@pytest.mark.integration
def test_intraday_check_writes_suggested_action_for_threshold_breach(
    factory: sessionmaker,
    fake_futu: FakeFutuClient,
    app_cfg: AppConfig,
    watchlist: WatchlistConfig,
) -> None:
    """Close < lower_threshold → strategy_lite suggests BUY → row has suggested_action=BUY."""
    with session_scope(factory) as s:
        s.add(
            Symbol(
                code="US.AAPL",
                name="Apple",
                upper_threshold=200.0,
                lower_threshold=170.0,
            )
        )

    base_ts = datetime(2026, 5, 2, 9, 30)
    candles = _make_candles(base_ts, 40)
    # Drop the last close below lower_threshold=170 to fire threshold_breach_lower
    candles[-1] = Candle(
        code="US.AAPL",
        ts=candles[-1].ts,
        open=180.0,
        high=181.0,
        low=160.0,
        close=165.0,
        volume=20_000,
        turnover=3.3e6,
    )
    fake_futu.set_kline("US.AAPL", "K_60M", candles)
    fake_futu.set_snapshot(
        Snapshot(
            code="US.AAPL",
            last_price=165.0,
            open_price=180.0,
            high_price=181.0,
            low_price=160.0,
            volume=12_000_000,
            turnover=2.0e9,
            update_time=base_ts + timedelta(hours=39),
        )
    )

    sent: list[dict[str, Any]] = []

    def sender(card, open_id, receiver_type):  # type: ignore[no-untyped-def]
        sent.append(card)
        return "om_x"

    out = run_intraday_check(
        client=fake_futu,
        factory=factory,
        cfg=app_cfg,
        watchlist=watchlist,
        send_card_fn=sender,
    )
    assert out.get("suggestions", 0) >= 1, out

    with session_scope(s_factory := factory) as s:
        rows = (
            s.query(SignalRow)
            .filter(SignalRow.signal_type == "threshold_breach_lower")
            .all()
        )
        assert len(rows) == 1
        assert rows[0].suggested_action == "BUY"
        assert rows[0].suggested_qty == 100
        assert rows[0].status == "pending"

    # Card should embed the trade confirm command
    assert any(
        "trade confirm" in str(card) for card in sent
    ), f"no trade-confirm tip in any pushed card: {sent}"


@pytest.mark.integration
def test_intraday_check_skips_unknown_symbol_in_db(
    factory: sessionmaker,
    fake_futu: FakeFutuClient,
    app_cfg: AppConfig,
    watchlist: WatchlistConfig,
) -> None:
    """Symbol in watchlist but missing in DB → quote upsert skipped, no crash."""
    base_ts = datetime(2026, 5, 2, 9, 30)
    fake_futu.set_kline("US.AAPL", "K_60M", _make_candles(base_ts, 40))
    fake_futu.set_snapshot(
        Snapshot(
            code="US.AAPL",
            last_price=180.0,
            open_price=179.0,
            high_price=181.0,
            low_price=178.5,
            volume=10_000,
            turnover=1.8e6,
            update_time=base_ts + timedelta(hours=39),
        )
    )

    out = run_intraday_check(
        client=fake_futu,
        factory=factory,
        cfg=app_cfg,
        watchlist=watchlist,
        send_card_fn=lambda *args: "om_skip",
    )
    assert out["quotes"] == 0
