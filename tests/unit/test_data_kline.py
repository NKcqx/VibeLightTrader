from __future__ import annotations

from datetime import datetime

from equity_monitor.data.kline import fetch_kline_df, fetch_klines_multi
from equity_monitor.futu_client import Candle, FakeFutuClient, FREQ_TO_KTYPE


def _candles(n: int = 6) -> list[Candle]:
    return [
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
        for h in range(10, 10 + n)
    ]


def test_fetch_kline_returns_dataframe(fake_futu: FakeFutuClient) -> None:
    fake_futu.set_kline("US.AAPL", "K_60M", _candles())
    df = fetch_kline_df(fake_futu, "US.AAPL", ktype="K_60M", limit=6)
    assert list(df.columns) == [
        "open",
        "high",
        "low",
        "close",
        "volume",
        "turnover",
    ]
    assert len(df) == 6
    assert df.index.name == "ts"
    assert df["close"].iloc[-1] == 180.5 + 15


def test_fetch_kline_empty_when_no_data(fake_futu: FakeFutuClient) -> None:
    df = fetch_kline_df(fake_futu, "US.UNKNOWN", ktype="K_60M", limit=10)
    assert df.empty
    assert list(df.columns) == [
        "open",
        "high",
        "low",
        "close",
        "volume",
        "turnover",
    ]


def test_fetch_kline_respects_limit(fake_futu: FakeFutuClient) -> None:
    fake_futu.set_kline("US.AAPL", "K_60M", _candles(n=10))
    df = fetch_kline_df(fake_futu, "US.AAPL", ktype="K_60M", limit=4)
    assert len(df) == 4
    assert df.index.is_monotonic_increasing


def test_fetch_kline_sorted_by_ts(fake_futu: FakeFutuClient) -> None:
    """If candles arrive out of order, the DataFrame should be sorted ascending."""
    candles = _candles()
    reversed_candles = list(reversed(candles))
    fake_futu.set_kline("US.AAPL", "K_60M", reversed_candles)
    df = fetch_kline_df(fake_futu, "US.AAPL", ktype="K_60M", limit=6)
    assert df.index.is_monotonic_increasing
    assert df["close"].iloc[0] == 180.5 + 10
    assert df["close"].iloc[-1] == 180.5 + 15


def test_freq_to_ktype_table_covers_supported_frequencies() -> None:
    assert FREQ_TO_KTYPE["1m"] == "K_1M"
    assert FREQ_TO_KTYPE["5m"] == "K_5M"
    assert FREQ_TO_KTYPE["15m"] == "K_15M"
    assert FREQ_TO_KTYPE["30m"] == "K_30M"
    assert FREQ_TO_KTYPE["60m"] == "K_60M"
    assert FREQ_TO_KTYPE["D"] == "K_DAY"
    assert FREQ_TO_KTYPE["W"] == "K_WEEK"


def test_fetch_klines_multi_returns_dict_keyed_by_freq(fake_futu: FakeFutuClient) -> None:
    # populate three frequencies
    fake_futu.set_kline("US.AAPL", "K_60M", _candles(6))
    fake_futu.set_kline("US.AAPL", "K_DAY", _candles(8))
    fake_futu.set_kline("US.AAPL", "K_WEEK", _candles(4))

    out = fetch_klines_multi(fake_futu, "US.AAPL", freqs=["60m", "D", "W"], limit=200)
    assert set(out.keys()) == {"60m", "D", "W"}
    assert len(out["60m"]) == 6
    assert len(out["D"]) == 8
    assert len(out["W"]) == 4
    for df in out.values():
        assert list(df.columns) == ["open", "high", "low", "close", "volume", "turnover"]


def test_fetch_klines_multi_skips_unknown_freq(fake_futu: FakeFutuClient) -> None:
    fake_futu.set_kline("US.AAPL", "K_60M", _candles(3))
    out = fetch_klines_multi(
        fake_futu, "US.AAPL", freqs=["60m", "bogus", "D"], limit=200
    )
    # bogus is silently skipped (not present in FREQ_TO_KTYPE)
    assert "bogus" not in out
    assert "60m" in out
    # D was not pre-populated, so it's an empty DF — present but empty
    assert "D" in out
    assert out["D"].empty
