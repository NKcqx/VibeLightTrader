"""Single source of truth for executing a Signal-driven paper trade.

Used by:
- `vibe-trader trade confirm <signal_id>` (manual via CLI)
- `scheduler/jobs.py:run_intraday_check` (auto when `cfg.trader.auto_execute=True`)

Pure function: takes an open SQLAlchemy session, a Signal row, a Symbol
row, qty, and a PaperTrader. Places the order, persists Trade + updates
Position + mutates Signal, returns trade.id. Raises SignalExecutionError
on broker rejection or invalid signal state. Does NOT close the trader
(caller's responsibility).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from vibe_trader.models import Position, Trade
from vibe_trader.models import Signal as SignalRow
from vibe_trader.models import Symbol


class SignalExecutionError(RuntimeError):
    """Raised when a Signal cannot be executed as a paper trade.

    Distinguishes broker-side rejection (insufficient qty, mark price
    missing, etc.) from invalid input (non-actionable side).
    """


def execute_signal_trade(
    s: Session,
    sig: SignalRow,
    sym: Symbol,
    qty: int,
    trader: Any,
) -> int:
    """Place order via `trader`, persist Trade, update Position, mutate sig.

    Args:
        s: Open SQLAlchemy session. Caller is responsible for commit.
        sig: Signal row to execute against (its `suggested_action` drives
            the side; `signal_id` is recorded on the Trade).
        sym: Symbol row matching `sig.symbol_id`.
        qty: Quantity to trade. Must be >0. (Caller decides whether to
            use sig.suggested_qty or override.)
        trader: PaperTrader implementation (FakePaperTrader or
            OpenDSecTrader). Must support `place_order`.

    Returns:
        The newly-inserted Trade row's id.

    Raises:
        SignalExecutionError: if side is not BUY/SELL, qty<=0, or the
            broker rejects the order. On rejection, sig.status is
            mutated to "cancelled" before raising.
    """
    side = sig.suggested_action
    if side not in ("BUY", "SELL"):
        raise SignalExecutionError(
            f"signal {sig.id} suggested_action={side!r} is not actionable"
        )
    if qty <= 0:
        raise SignalExecutionError(
            f"qty must be positive (got {qty}) for signal {sig.id}"
        )

    result = trader.place_order(code=sym.code, side=side, qty=qty)
    if result.status == "REJECTED":
        sig.status = "cancelled"
        raise SignalExecutionError(
            f"order rejected by paper broker: {result.error}"
        )

    # Persist the Trade row regardless of fill state — it records the
    # decision history. For FILLED orders we use the broker-reported
    # filled_qty / avg_fill_price; for PENDING ones (e.g. after-hours
    # SIMULATE that queues the order until next session) we record the
    # requested qty and leave price at 0.0 so the row is faithful to the
    # decision but downstream P&L code can detect "not yet filled".
    trade_qty = result.filled_qty if result.status == "FILLED" else qty
    trade_price = result.avg_fill_price if result.status == "FILLED" else 0.0
    trade_row = Trade(
        symbol_id=sym.id,
        ts=result.submitted_at,
        side=side,
        qty=trade_qty,
        price=trade_price,
        futu_order_id=result.order_id,
        signal_id=sig.id,
        status=result.status,
    )
    s.add(trade_row)
    s.flush()  # populate trade_row.id

    # Position is mutated ONLY on FILLED orders. PENDING orders sit in the
    # broker's queue; their effect on the position will be applied by a
    # future fill-confirmation pass (TODO future work). This avoids the
    # stale-position bug where after-hours unfilled orders inflate qty
    # with avg_cost=0 and corrupt P&L.
    if result.status == "FILLED":
        pos = s.query(Position).filter(Position.symbol_id == sym.id).one_or_none()
        if side == "BUY":
            if pos is None:
                s.add(
                    Position(
                        symbol_id=sym.id,
                        qty=trade_qty,
                        avg_cost=result.avg_fill_price,
                    )
                )
            else:
                new_qty = pos.qty + trade_qty
                pos.avg_cost = (
                    (pos.qty * pos.avg_cost)
                    + (trade_qty * result.avg_fill_price)
                ) / new_qty
                pos.qty = new_qty
        else:  # SELL
            assert pos is not None and pos.qty >= trade_qty, (
                "oversold past broker check?"
            )
            realized = (result.avg_fill_price - pos.avg_cost) * trade_qty
            pos.realized_pnl = (pos.realized_pnl or 0.0) + realized
            pos.qty -= trade_qty
            if pos.qty == 0:
                pos.avg_cost = 0.0

    sig.status = "executed"
    sig.executed_trade_id = trade_row.id
    return trade_row.id
