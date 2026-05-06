from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy.orm import sessionmaker

from vibe_trader.config import (
    AppConfig,
    DatabaseConfig,
    LarkConfig,
    LarkReceiver,
    LoggingConfig,
    OpenDConfig,
    SchedulerConfig,
    SignalsConfig,
)
from vibe_trader.db import init_schema, make_engine, make_sessionmaker, session_scope
from vibe_trader.events.enrich import build_watchlist_rows
from vibe_trader.futu_client import Candle, FakeFutuClient, Snapshot
from vibe_trader.models import Symbol


@pytest.fixture
def factory(tmp_path: Path) -> sessionmaker:
    engine = make_engine(str(tmp_path / "x.db"), wal_mode=False)
    init_schema(engine)
    return make_sessionmaker(engine)


@pytest.fixture
def cfg() -> AppConfig:
    return AppConfig(
        opend=OpenDConfig(host="127.0.0.1", port=11111),
        database=DatabaseConfig(path=":memory:", wal_mode=False),
        scheduler=SchedulerConfig(timezone="UTC", jobs={}),
        lark=LarkConfig(
            cli_path="lark-cli",
            identity="bot",
            receiver=LarkReceiver(type="user", open_id="ou_user1"),
        ),
        signals=SignalsConfig(),
        logging=LoggingConfig(),
    )


@pytest.fixture
def fake_client() -> FakeFutuClient:
    c = FakeFutuClient()
    c.set_snapshot(Snapshot(
        code="US.AAPL", last_price=280.0, open_price=275.0,
        high_price=281.0, low_price=274.0, volume=1_000_000, turnover=2.8e8,
        update_time=datetime(2026, 5, 2, 19, 0, tzinfo=timezone.utc),
    ))
    # 50 candles ~$270 with slight upward drift
    candles: list[Candle] = []
    for i in range(50):
        ts = datetime(2026, 4, 1, 14 + i % 7, tzinfo=timezone.utc)
        close = 270.0 + i * 0.2
        candles.append(Candle(
            code="US.AAPL", ts=ts,
            open=close - 0.1, high=close + 0.5, low=close - 0.5, close=close,
            volume=10_000, turnover=close * 10_000,
        ))
    c.set_kline("US.AAPL", "K_60M", candles)
    return c


def test_build_rows_empty_watchlist(factory: sessionmaker, cfg: AppConfig) -> None:
    rows, n = build_watchlist_rows(cfg=cfg, factory=factory, client=FakeFutuClient())
    assert rows == []
    assert n == 0


def test_build_rows_renders_price_and_thresholds(
    factory: sessionmaker, cfg: AppConfig, fake_client: FakeFutuClient
) -> None:
    with session_scope(factory) as s:
        s.add(Symbol(
            code="US.AAPL", name="Apple",
            upper_threshold=200.0, lower_threshold=165.0,
        ))

    rows, n = build_watchlist_rows(cfg=cfg, factory=factory, client=fake_client)
    assert n == 1
    body = rows[0].body_md
    assert "US.AAPL" in body and "Apple" in body
    assert "$280.00" in body  # current price from snapshot
    # 280 vs open 275 = +1.82% intraday
    assert "▲" in body
    assert "上限" in body and "200" in body
    # 280 > 200 → breached, should show 🔴
    assert "🔴" in body
    # Indicators: at least RSI / MACD / BOLL keywords present
    assert "📊" in body


def test_build_rows_handles_missing_snapshot_gracefully(
    factory: sessionmaker, cfg: AppConfig
) -> None:
    """Snapshot fetch failure should NOT crash; row still renders w/ thresholds."""
    with session_scope(factory) as s:
        s.add(Symbol(code="US.AAPL", name="Apple", upper_threshold=200.0, lower_threshold=None))

    class _FailingClient(FakeFutuClient):
        def snapshot(self, codes):  # type: ignore[override]
            raise RuntimeError("OpenD down")

    rows, n = build_watchlist_rows(cfg=cfg, factory=factory, client=_FailingClient())
    assert n == 1
    body = rows[0].body_md
    assert "报价获取失败" in body
    assert "上限" in body  # thresholds still show


def test_build_rows_handles_missing_kline_gracefully(
    factory: sessionmaker, cfg: AppConfig
) -> None:
    """Kline fetch missing → indicator line falls back to '暂无 K 线数据'."""
    with session_scope(factory) as s:
        s.add(Symbol(code="US.AAPL", name="Apple", upper_threshold=None, lower_threshold=None))

    client = FakeFutuClient()
    client.set_snapshot(Snapshot(
        code="US.AAPL", last_price=280.0, open_price=275.0,
        high_price=281, low_price=274, volume=0, turnover=0,
        update_time=datetime(2026, 5, 2, 19, 0, tzinfo=timezone.utc),
    ))
    # No klines set
    rows, n = build_watchlist_rows(cfg=cfg, factory=factory, client=client)
    assert n == 1
    body = rows[0].body_md
    assert "$280.00" in body
    assert "暂无 K 线数据" in body or "指标暂不可用" in body
