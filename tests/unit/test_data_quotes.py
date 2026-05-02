from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import sessionmaker

from equity_monitor.data.quotes import sync_snapshots
from equity_monitor.db import session_scope
from equity_monitor.futu_client import FakeFutuClient, Snapshot
from equity_monitor.models import Quote, Symbol


def _make_snap(code: str = "US.AAPL", ts: datetime | None = None) -> Snapshot:
    return Snapshot(
        code=code,
        last_price=182.3,
        open_price=180.0,
        high_price=183.0,
        low_price=179.5,
        volume=12_000_000,
        turnover=2.184e9,
        update_time=ts or datetime(2026, 5, 2, 14, 30),
    )


def test_sync_snapshots_inserts_quote(
    factory: sessionmaker, fake_futu: FakeFutuClient
) -> None:
    with session_scope(factory) as s:
        s.add(Symbol(code="US.AAPL", name="Apple"))

    fake_futu.set_snapshot(_make_snap())
    inserted = sync_snapshots(fake_futu, factory, codes=["US.AAPL"])
    assert inserted == 1

    with session_scope(factory) as s:
        q = s.query(Quote).one()
        assert q.close == 182.3
        assert q.open == 180.0


def test_sync_snapshots_idempotent(
    factory: sessionmaker, fake_futu: FakeFutuClient
) -> None:
    with session_scope(factory) as s:
        s.add(Symbol(code="US.AAPL", name="Apple"))

    fake_futu.set_snapshot(_make_snap())
    n1 = sync_snapshots(fake_futu, factory, codes=["US.AAPL"])
    n2 = sync_snapshots(fake_futu, factory, codes=["US.AAPL"])
    assert n1 == 1
    assert n2 == 0

    with session_scope(factory) as s:
        assert s.query(Quote).count() == 1


def test_sync_snapshots_skips_unknown_symbol(
    factory: sessionmaker, fake_futu: FakeFutuClient
) -> None:
    """Snapshot for a symbol not in DB should be skipped, not crash."""
    fake_futu.set_snapshot(_make_snap(code="US.UNKNOWN"))
    inserted = sync_snapshots(fake_futu, factory, codes=["US.UNKNOWN"])
    assert inserted == 0


def test_sync_snapshots_two_symbols_partial_match(
    factory: sessionmaker, fake_futu: FakeFutuClient
) -> None:
    """Only known symbols get inserted; unknown silently skipped."""
    with session_scope(factory) as s:
        s.add(Symbol(code="US.AAPL", name="Apple"))

    fake_futu.set_snapshot(_make_snap(code="US.AAPL"))
    fake_futu.set_snapshot(_make_snap(code="US.UNKNOWN"))
    inserted = sync_snapshots(
        fake_futu, factory, codes=["US.AAPL", "US.UNKNOWN"]
    )
    assert inserted == 1
