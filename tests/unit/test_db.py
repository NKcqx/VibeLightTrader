from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from vibe_trader.db import session_scope
from vibe_trader.models import Quote, Symbol


def test_can_insert_symbol_and_quote(factory: sessionmaker[Session]) -> None:
    with session_scope(factory) as s:
        sym = Symbol(code="US.AAPL", name="Apple")
        s.add(sym)
        s.flush()
        s.add(
            Quote(
                symbol_id=sym.id,
                ts=datetime(2026, 5, 2, 14, 30, tzinfo=timezone.utc).replace(
                    tzinfo=None
                ),
                open=180.0,
                high=183.0,
                low=179.5,
                close=182.3,
                volume=12_000_000,
                turnover=2_184_000_000.0,
            )
        )

    with session_scope(factory) as s:
        quotes = s.query(Quote).all()
        assert len(quotes) == 1
        assert quotes[0].close == 182.3


def test_unique_constraint_quote(factory: sessionmaker[Session]) -> None:
    ts = datetime(2026, 5, 2, 14, 30)
    with session_scope(factory) as s:
        sym = Symbol(code="US.AAPL", name="Apple")
        s.add(sym)
        s.flush()
        s.add(
            Quote(
                symbol_id=sym.id,
                ts=ts,
                open=1,
                high=1,
                low=1,
                close=1,
                volume=1,
                turnover=1,
            )
        )

    with pytest.raises(IntegrityError):
        with session_scope(factory) as s:
            sym = s.query(Symbol).first()
            assert sym is not None
            s.add(
                Quote(
                    symbol_id=sym.id,
                    ts=ts,
                    open=2,
                    high=2,
                    low=2,
                    close=2,
                    volume=2,
                    turnover=2,
                )
            )
