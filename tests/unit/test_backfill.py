from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import sessionmaker

from vibe_trader.data.backfill import backfill_all, backfill_symbol
from vibe_trader.db import session_scope
from vibe_trader.futu_client import Candle, FakeFutuClient
from vibe_trader.models import Indicator, Quote, Symbol


def _candles(code: str, n: int) -> list[Candle]:
    base = datetime(2026, 4, 1, 9, 30)
    return [
        Candle(
            code=code,
            ts=base + timedelta(hours=h),
            open=100.0 + h * 0.1,
            high=101.0 + h * 0.1,
            low=99.0 + h * 0.1,
            close=100.5 + h * 0.1,
            volume=10_000,
            turnover=1.0e6,
        )
        for h in range(n)
    ]


def test_backfill_symbol_inserts_quotes_and_indicators(
    factory: sessionmaker, fake_futu: FakeFutuClient
) -> None:
    with session_scope(factory) as s:
        s.add(Symbol(code="US.AAPL", name="Apple"))
    fake_futu.set_kline("US.AAPL", "K_60M", _candles("US.AAPL", 50))

    out = backfill_symbol(
        client=fake_futu, factory=factory, code="US.AAPL", days=10
    )
    assert out["quotes"] == 50
    assert out["indicators"] == 50

    with session_scope(factory) as s:
        assert s.query(Quote).count() == 50
        assert s.query(Indicator).count() == 50


def test_backfill_symbol_idempotent_on_repeat(
    factory: sessionmaker, fake_futu: FakeFutuClient
) -> None:
    with session_scope(factory) as s:
        s.add(Symbol(code="US.AAPL", name="Apple"))
    fake_futu.set_kline("US.AAPL", "K_60M", _candles("US.AAPL", 30))

    backfill_symbol(client=fake_futu, factory=factory, code="US.AAPL", days=5)
    out2 = backfill_symbol(
        client=fake_futu, factory=factory, code="US.AAPL", days=5
    )
    assert out2 == {"quotes": 0, "indicators": 0}
    with session_scope(factory) as s:
        assert s.query(Quote).count() == 30


def test_backfill_symbol_unknown_returns_zeros(
    factory: sessionmaker, fake_futu: FakeFutuClient
) -> None:
    """If symbol row is absent in DB, backfill must skip cleanly without raising."""
    fake_futu.set_kline("US.GHOST", "K_60M", _candles("US.GHOST", 10))
    out = backfill_symbol(
        client=fake_futu, factory=factory, code="US.GHOST", days=2
    )
    assert out == {"quotes": 0, "indicators": 0}
    with session_scope(factory) as s:
        assert s.query(Quote).count() == 0


def test_backfill_symbol_empty_kline_returns_zeros(
    factory: sessionmaker, fake_futu: FakeFutuClient
) -> None:
    with session_scope(factory) as s:
        s.add(Symbol(code="US.AAPL", name="Apple"))
    out = backfill_symbol(
        client=fake_futu, factory=factory, code="US.AAPL", days=10
    )
    assert out == {"quotes": 0, "indicators": 0}


def test_backfill_indicators_handles_nan_warmup(
    factory: sessionmaker, fake_futu: FakeFutuClient
) -> None:
    """The first ~26 bars produce NaN indicators (warmup); they must persist as NULL."""
    with session_scope(factory) as s:
        s.add(Symbol(code="US.AAPL", name="Apple"))
    fake_futu.set_kline("US.AAPL", "K_60M", _candles("US.AAPL", 40))

    out = backfill_symbol(
        client=fake_futu, factory=factory, code="US.AAPL", days=6
    )
    assert out["indicators"] == 40
    with session_scope(factory) as s:
        first = (
            s.query(Indicator).order_by(Indicator.ts).first()
        )
        assert first is not None
        assert first.macd is None  # macd needs 26 bars warmup
        last = s.query(Indicator).order_by(Indicator.ts.desc()).first()
        assert last is not None
        assert last.rsi_14 is not None
        assert last.macd is not None


def test_backfill_all_aggregates_per_symbol(
    factory: sessionmaker, fake_futu: FakeFutuClient
) -> None:
    with session_scope(factory) as s:
        s.add(Symbol(code="US.AAPL", name="Apple"))
        s.add(Symbol(code="US.NVDA", name="NVIDIA"))
    fake_futu.set_kline("US.AAPL", "K_60M", _candles("US.AAPL", 20))
    fake_futu.set_kline("US.NVDA", "K_60M", _candles("US.NVDA", 25))

    out = backfill_all(
        client=fake_futu,
        factory=factory,
        codes=["US.AAPL", "US.NVDA"],
        days=4,
    )
    assert out["US.AAPL"]["quotes"] == 20
    assert out["US.NVDA"]["quotes"] == 25
