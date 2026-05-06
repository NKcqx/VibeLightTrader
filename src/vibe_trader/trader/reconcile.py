"""Backfill MARKET-order fill prices from the broker.

When `OpenDSecTrader.place_order` returns from a MARKET submit, OpenD has
typically only acknowledged the order (status SUBMITTED, dealt_avg_price=0)
and not yet reported the actual fill. We persist the Trade row anyway with
`price=0.0` and `status=PENDING` so the decision audit trail is complete,
but until reconcile runs, downstream consumers (chart markers, avg-cost,
PnL) see the placeholder 0.

`reconcile_pending_fills` queries the broker for orders within a window,
matches them against PENDING+price=0 Trade rows by `futu_order_id`, and
writes the actual fill price + status back. Designed to be called from
the `chart` CLI path (cheap if there's nothing to fix), but is itself
trader-agnostic (any `PaperTrader` with `query_history_orders`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import sessionmaker

from vibe_trader.db import session_scope
from vibe_trader.models import Trade
from vibe_trader.trader.paper import PaperTrader


@dataclass(frozen=True)
class ReconcileResult:
    candidates: int  # PENDING+price=0 rows in window
    matched: int     # of those, found a broker order with matching id
    updated: int     # of those, broker now reports a non-zero fill price
    errors: int      # broker query failed (network/auth/etc.)


def reconcile_pending_fills(
    factory: sessionmaker,
    trader: PaperTrader,
    *,
    since: datetime | None = None,
    now: datetime | None = None,
) -> ReconcileResult:
    """Heal Trade rows whose price is still 0 because the MARKET fill
    landed asynchronously after we wrote the row.

    `since` defaults to `now - 30 days` (covers the same window as the
    chart command); broader windows are fine but slow if there are many
    orders in history. Returns counts so the caller can log a one-line
    summary instead of spamming per-order updates.
    """
    now = now or datetime.now(tz=timezone.utc)
    since = since or (now - timedelta(days=30))

    with session_scope(factory) as session:
        candidates = (
            session.query(Trade)
            .filter(
                Trade.status == "PENDING",
                Trade.price == 0.0,
                Trade.ts >= since,
                Trade.futu_order_id.isnot(None),
            )
            .all()
        )
        if not candidates:
            return ReconcileResult(0, 0, 0, 0)

        try:
            broker_orders = trader.query_history_orders(start=since, end=now)
        except Exception:
            return ReconcileResult(len(candidates), 0, 0, 1)

        # Index by order_id for O(1) lookup. Broker may have multiple
        # entries per id if futu re-emits — last one wins (the more
        # recent state).
        by_id = {o.order_id: o for o in broker_orders}

        matched = 0
        updated = 0
        for trade in candidates:
            o = by_id.get(str(trade.futu_order_id))
            if o is None:
                continue
            matched += 1
            if o.avg_fill_price > 0 and o.filled_qty > 0:
                trade.price = o.avg_fill_price
                trade.qty = o.filled_qty
                trade.status = "FILLED" if o.status == "FILLED" else "PARTIAL"
                updated += 1
            elif o.status == "CANCELLED":
                trade.status = "CANCELLED"
                # Keep price at 0; UI should drop these anyway.

        return ReconcileResult(
            candidates=len(candidates),
            matched=matched,
            updated=updated,
            errors=0,
        )
