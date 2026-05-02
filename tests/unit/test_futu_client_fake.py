from __future__ import annotations

from datetime import datetime

from equity_monitor.futu_client import Candle, FakeFutuClient, Snapshot


def test_fake_snapshot_roundtrip(fake_futu: FakeFutuClient) -> None:
    fake_futu.set_snapshot(
        Snapshot(
            code="US.AAPL",
            last_price=182.3,
            open_price=180.0,
            high_price=183.0,
            low_price=179.5,
            volume=12_000_000,
            turnover=2.184e9,
            update_time=datetime(2026, 5, 2, 14, 30),
        )
    )
    out = fake_futu.snapshot(["US.AAPL"])
    assert len(out) == 1 and out[0].last_price == 182.3


def test_fake_kline_limit(fake_futu: FakeFutuClient) -> None:
    candles = [
        Candle(
            code="US.AAPL",
            ts=datetime(2026, 5, 2, h, 30),
            open=180.0 + h,
            high=181.0 + h,
            low=179.0 + h,
            close=180.5 + h,
            volume=10_000,
            turnover=1.8e6,
        )
        for h in range(10, 16)
    ]
    fake_futu.set_kline("US.AAPL", "K_60M", candles)
    out = fake_futu.kline("US.AAPL", ktype="K_60M", limit=3)
    assert len(out) == 3
    assert [c.ts.hour for c in out] == [13, 14, 15]


def test_fake_snapshot_missing_code_skipped(fake_futu: FakeFutuClient) -> None:
    out = fake_futu.snapshot(["US.AAPL", "US.UNKNOWN"])
    assert out == []


def test_fake_close_marks_flag(fake_futu: FakeFutuClient) -> None:
    assert fake_futu.closed is False
    fake_futu.close()
    assert fake_futu.closed is True
