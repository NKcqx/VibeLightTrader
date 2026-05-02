from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import sessionmaker

from equity_monitor.db import session_scope
from equity_monitor.futu_client import FutuClient
from equity_monitor.models import Quote, Symbol


def sync_snapshots(
    client: FutuClient,
    factory: sessionmaker,
    *,
    codes: Sequence[str],
) -> int:
    """Pull snapshots for `codes` and upsert into `quotes`.

    Returns the number of rows actually inserted (duplicates by (symbol_id, ts)
    are skipped via SQLite ON CONFLICT DO NOTHING).
    """
    snaps = client.snapshot(codes)
    inserted = 0
    with session_scope(factory) as session:
        sym_map = {
            s.code: s.id
            for s in session.query(Symbol).filter(Symbol.code.in_(codes)).all()
        }
        for snap in snaps:
            sym_id = sym_map.get(snap.code)
            if sym_id is None:
                continue
            stmt = (
                insert(Quote)
                .values(
                    symbol_id=sym_id,
                    ts=snap.update_time,
                    open=snap.open_price,
                    high=snap.high_price,
                    low=snap.low_price,
                    close=snap.last_price,
                    volume=snap.volume,
                    turnover=snap.turnover,
                )
                .on_conflict_do_nothing(index_elements=["symbol_id", "ts"])
            )
            result = session.execute(stmt)
            if result.rowcount > 0:
                inserted += 1
    return inserted
