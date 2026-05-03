"""Verify run_intraday_check fires send_image_fn when a signal is alerted."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from equity_monitor.config import (
    AppConfig,
    DatabaseConfig,
    JobCron,
    LarkConfig,
    LarkReceiver,
    LoggingConfig,
    OpenDConfig,
    SchedulerConfig,
    SignalsConfig,
    SymbolConfig,
    WatchlistConfig,
)
from equity_monitor.futu_client import Candle, FakeFutuClient, Snapshot
from equity_monitor.models import Symbol, Trade
from equity_monitor.scheduler.jobs import run_intraday_check


def _make_cfg(tmp_path: Path) -> AppConfig:
    return AppConfig(
        opend=OpenDConfig(),
        database=DatabaseConfig(path=str(tmp_path / "test.db"), wal_mode=False),
        lark=LarkConfig(
            receiver=LarkReceiver(open_id="ou_test", type="user"),
        ),
        signals=SignalsConfig(
            macd_fast=12,
            macd_slow=26,
            macd_signal=9,
            bollinger_period=20,
            bollinger_std=2.0,
            rsi_overbought=70.0,
            rsi_oversold=30.0,
            dedupe_window_minutes=15,
        ),
        scheduler=SchedulerConfig(
            timezone="UTC",
            jobs={"intraday_check": JobCron(cron="*/5 * * * *")},
        ),
        logging=LoggingConfig(level="INFO", file=None),
    )


def _make_watchlist() -> WatchlistConfig:
    return WatchlistConfig(
        symbols=[
            SymbolConfig(
                code="US.AAPL",
                name="Apple",
                upper_threshold=200.0,
                lower_threshold=100.0,
            ),
        ]
    )


def _populate_kline(client: FakeFutuClient, code: str, base_price: float) -> None:
    """Pre-load 60 hourly bars trending UP to trip RSI / threshold rules."""
    candles = []
    base = datetime(2026, 5, 1, 9, 30, tzinfo=timezone.utc)
    for i in range(60):
        p = base_price + i * 0.5
        candles.append(
            Candle(
                code=code,
                ts=base + timedelta(hours=i),
                open=p,
                high=p + 1.0,
                low=p - 1.0,
                close=p + 0.5,
                volume=10_000,
                turnover=p * 10_000,
            )
        )
    client.set_kline(code, "K_60M", candles)


def _populate_snapshot(client: FakeFutuClient, code: str, last: float) -> None:
    client.set_snapshot(
        Snapshot(
            code=code,
            last_price=last,
            open_price=last - 1.0,
            high_price=last + 2.0,
            low_price=last - 3.0,
            volume=100_000,
            turnover=last * 100_000,
            update_time=datetime(2026, 5, 1, 16, 0, tzinfo=timezone.utc),
        )
    )


@pytest.fixture
def fake_client() -> FakeFutuClient:
    c = FakeFutuClient()
    _populate_kline(c, "US.AAPL", base_price=150.0)
    _populate_snapshot(c, "US.AAPL", last=250.0)
    return c


@pytest.mark.integration
def test_send_image_fn_fired_after_card_when_signal_triggers(
    fake_client: FakeFutuClient,
    factory: sessionmaker,
    tmp_path: Path,
) -> None:
    with factory() as session:
        sym = Symbol(
            code="US.AAPL",
            name="Apple",
            upper_threshold=200.0,
            lower_threshold=100.0,
        )
        session.add(sym)
        session.flush()
        session.add(
            Trade(
                symbol_id=sym.id,
                ts=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
                side="BUY",
                qty=100,
                price=150.0,
                status="filled",
            )
        )
        session.commit()

    sent_cards: list = []
    sent_images: list = []

    def fake_card_sender(card, open_id, receiver_type):  # type: ignore[no-untyped-def]
        sent_cards.append({"card": card, "to": open_id, "rt": receiver_type})
        return "om_card_xxx"

    def fake_image_sender(path, open_id, receiver_type):  # type: ignore[no-untyped-def]
        sent_images.append({"path": path, "to": open_id, "rt": receiver_type})
        return "om_img_xxx"

    res = run_intraday_check(
        client=fake_client,
        factory=factory,
        cfg=_make_cfg(tmp_path),
        watchlist=_make_watchlist(),
        send_card_fn=fake_card_sender,
        send_image_fn=fake_image_sender,
        snapshot_dir=tmp_path,
    )

    assert res["pushed"] >= 1, "card should have been pushed for threshold breach"
    assert len(sent_cards) >= 1
    assert len(sent_images) >= 1, "image should follow the card"
    img = sent_images[0]
    assert img["to"] == "ou_test"
    assert img["rt"] == "user"
    p = img["path"]
    assert isinstance(p, Path)
    assert p.exists()
    assert p.suffix == ".png"
    assert p.stat().st_size > 1024


@pytest.mark.integration
def test_send_image_fn_skipped_when_none(
    fake_client: FakeFutuClient,
    factory: sessionmaker,
    tmp_path: Path,
) -> None:
    with factory() as session:
        session.add(
            Symbol(
                code="US.AAPL",
                name="Apple",
                upper_threshold=200.0,
                lower_threshold=100.0,
            )
        )
        session.commit()

    sent_cards: list = []

    def fake_card_sender(card, open_id, receiver_type):  # type: ignore[no-untyped-def]
        sent_cards.append(card)
        return "om_card_xxx"

    res = run_intraday_check(
        client=fake_client,
        factory=factory,
        cfg=_make_cfg(tmp_path),
        watchlist=_make_watchlist(),
        send_card_fn=fake_card_sender,
        send_image_fn=None,
        snapshot_dir=tmp_path,
    )

    assert res["pushed"] >= 1
    assert not list(tmp_path.glob("*.png")), (
        "snapshot_dir should have no renders when send_image_fn is None"
    )


@pytest.mark.integration
def test_image_send_failure_does_not_block_alert(
    fake_client: FakeFutuClient,
    factory: sessionmaker,
    tmp_path: Path,
) -> None:
    """If send_image_fn raises, the card is still considered pushed."""
    with factory() as session:
        session.add(
            Symbol(
                code="US.AAPL",
                name="Apple",
                upper_threshold=200.0,
                lower_threshold=100.0,
            )
        )
        session.commit()

    def fake_card_sender(card, open_id, receiver_type):  # type: ignore[no-untyped-def]
        return "om_card_xxx"

    def angry_image_sender(*a: object, **kw: object) -> str:
        raise RuntimeError("boom")

    res = run_intraday_check(
        client=fake_client,
        factory=factory,
        cfg=_make_cfg(tmp_path),
        watchlist=_make_watchlist(),
        send_card_fn=fake_card_sender,
        send_image_fn=angry_image_sender,
        snapshot_dir=tmp_path,
    )
    assert res["pushed"] >= 1, "card-push count should be unaffected by image failure"
