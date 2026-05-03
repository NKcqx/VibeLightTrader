"""Paper-trading wrapper around Futu OpenSecTradeContext.

Phase 2 MVP. Three implementations of the `PaperTrader` Protocol:

- `OpenDSecTrader`: real Futu SIMULATE account via OpenD (requires OpenD
  logged in with paper trading enabled).
- `FakePaperTrader`: in-memory deterministic broker — used in all unit tests.

Defensive design: the real client refuses to operate on anything other than
`TrdEnv.SIMULATE` even if the underlying account context happens to default
otherwise. This is the single guard that prevents accidental real-money
trades during Phase 2.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Protocol

from tenacity import retry, stop_after_attempt, wait_exponential

OrderSide = Literal["BUY", "SELL"]
OrderType = Literal["MARKET", "LIMIT"]
OrderStatus = Literal["FILLED", "PENDING", "REJECTED", "CANCELLED"]


class PaperTradeError(RuntimeError):
    pass


@dataclass(frozen=True)
class PaperOrderResult:
    order_id: str
    status: OrderStatus
    code: str
    side: OrderSide
    requested_qty: int
    filled_qty: int
    avg_fill_price: float
    submitted_at: datetime
    error: str | None = None


@dataclass(frozen=True)
class PaperPosition:
    code: str
    qty: int
    avg_cost: float
    market_value: float | None = None
    unrealized_pnl: float | None = None


@dataclass(frozen=True)
class PaperOrder:
    """A historical paper order (today's order book entry)."""

    order_id: str
    code: str
    side: OrderSide
    qty: int
    price: float | None
    status: OrderStatus
    submitted_at: datetime
    filled_qty: int = 0
    avg_fill_price: float = 0.0


class PaperTrader(Protocol):
    """Minimal interface used by the rest of the system."""

    def place_order(
        self,
        *,
        code: str,
        side: OrderSide,
        qty: int,
        order_type: OrderType = "MARKET",
        limit_price: float | None = None,
    ) -> PaperOrderResult: ...

    def cancel_order(self, order_id: str) -> None: ...

    def query_positions(self) -> list[PaperPosition]: ...

    def query_today_orders(self) -> list[PaperOrder]: ...

    def close(self) -> None: ...


# ---------------------------------------------------------------------------
# In-memory fake (test-only)
# ---------------------------------------------------------------------------


@dataclass
class FakePaperTrader:
    """In-memory deterministic broker.

    Default behavior:
      - MARKET orders fill instantly at `mark_price[code]` (set via set_mark).
      - LIMIT orders are stored as PENDING (no auto-fill simulation in MVP).
      - Positions track weighted-average cost; FIFO realized P&L is computed
        in `trader/pnl.py`, NOT here (this stays pure-broker).
      - SELL of more than current qty short-fails as REJECTED with reason.
    """

    mark_price: dict[str, float] = field(default_factory=dict)
    _orders: list[PaperOrder] = field(default_factory=list)
    _positions: dict[str, PaperPosition] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    closed: bool = False

    # ---- test helpers -----------------------------------------------------

    def set_mark(self, code: str, price: float) -> None:
        self.mark_price[code] = price

    # ---- PaperTrader protocol --------------------------------------------

    def place_order(
        self,
        *,
        code: str,
        side: OrderSide,
        qty: int,
        order_type: OrderType = "MARKET",
        limit_price: float | None = None,
    ) -> PaperOrderResult:
        if self.closed:
            raise PaperTradeError("trader closed")
        if qty <= 0:
            return PaperOrderResult(
                order_id=_new_oid(),
                status="REJECTED",
                code=code,
                side=side,
                requested_qty=qty,
                filled_qty=0,
                avg_fill_price=0.0,
                submitted_at=datetime.now(tz=timezone.utc),
                error="qty must be positive",
            )

        with self._lock:
            now = datetime.now(tz=timezone.utc)
            oid = _new_oid()

            if order_type == "LIMIT":
                if limit_price is None or limit_price <= 0:
                    return PaperOrderResult(
                        order_id=oid,
                        status="REJECTED",
                        code=code,
                        side=side,
                        requested_qty=qty,
                        filled_qty=0,
                        avg_fill_price=0.0,
                        submitted_at=now,
                        error="LIMIT order requires positive limit_price",
                    )
                self._orders.append(
                    PaperOrder(
                        order_id=oid,
                        code=code,
                        side=side,
                        qty=qty,
                        price=limit_price,
                        status="PENDING",
                        submitted_at=now,
                    )
                )
                return PaperOrderResult(
                    order_id=oid,
                    status="PENDING",
                    code=code,
                    side=side,
                    requested_qty=qty,
                    filled_qty=0,
                    avg_fill_price=0.0,
                    submitted_at=now,
                )

            if code not in self.mark_price:
                return PaperOrderResult(
                    order_id=oid,
                    status="REJECTED",
                    code=code,
                    side=side,
                    requested_qty=qty,
                    filled_qty=0,
                    avg_fill_price=0.0,
                    submitted_at=now,
                    error=f"no mark price for {code}",
                )

            fill_price = self.mark_price[code]

            if side == "SELL":
                cur = self._positions.get(code)
                cur_qty = cur.qty if cur else 0
                if cur_qty < qty:
                    return PaperOrderResult(
                        order_id=oid,
                        status="REJECTED",
                        code=code,
                        side=side,
                        requested_qty=qty,
                        filled_qty=0,
                        avg_fill_price=0.0,
                        submitted_at=now,
                        error=f"insufficient qty: have {cur_qty}, want {qty}",
                    )
                new_qty = cur_qty - qty
                if new_qty == 0:
                    self._positions.pop(code, None)
                else:
                    self._positions[code] = PaperPosition(
                        code=code,
                        qty=new_qty,
                        avg_cost=cur.avg_cost,  # type: ignore[union-attr]
                    )
            else:  # BUY
                cur = self._positions.get(code)
                if cur is None:
                    self._positions[code] = PaperPosition(
                        code=code, qty=qty, avg_cost=fill_price
                    )
                else:
                    new_qty = cur.qty + qty
                    new_avg = (cur.qty * cur.avg_cost + qty * fill_price) / new_qty
                    self._positions[code] = PaperPosition(
                        code=code, qty=new_qty, avg_cost=new_avg
                    )

            self._orders.append(
                PaperOrder(
                    order_id=oid,
                    code=code,
                    side=side,
                    qty=qty,
                    price=fill_price,
                    status="FILLED",
                    submitted_at=now,
                    filled_qty=qty,
                    avg_fill_price=fill_price,
                )
            )
            return PaperOrderResult(
                order_id=oid,
                status="FILLED",
                code=code,
                side=side,
                requested_qty=qty,
                filled_qty=qty,
                avg_fill_price=fill_price,
                submitted_at=now,
            )

    def cancel_order(self, order_id: str) -> None:
        with self._lock:
            for i, o in enumerate(self._orders):
                if o.order_id == order_id:
                    if o.status != "PENDING":
                        raise PaperTradeError(
                            f"order {order_id} status={o.status}, cannot cancel"
                        )
                    self._orders[i] = PaperOrder(
                        order_id=o.order_id,
                        code=o.code,
                        side=o.side,
                        qty=o.qty,
                        price=o.price,
                        status="CANCELLED",
                        submitted_at=o.submitted_at,
                    )
                    return
            raise PaperTradeError(f"order {order_id} not found")

    def query_positions(self) -> list[PaperPosition]:
        with self._lock:
            out: list[PaperPosition] = []
            for code, pos in self._positions.items():
                mark = self.mark_price.get(code)
                if mark is not None:
                    mv = pos.qty * mark
                    upnl = (mark - pos.avg_cost) * pos.qty
                    out.append(
                        PaperPosition(
                            code=code,
                            qty=pos.qty,
                            avg_cost=pos.avg_cost,
                            market_value=mv,
                            unrealized_pnl=upnl,
                        )
                    )
                else:
                    out.append(pos)
            return out

    def query_today_orders(self) -> list[PaperOrder]:
        with self._lock:
            return list(self._orders)

    def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# Real OpenD-backed paper trader
# ---------------------------------------------------------------------------


class OpenDSecTrader:
    """Real Futu paper-trading client backed by `futu.OpenSecTradeContext`.

    SAFETY: refuses to operate unless a SIMULATE account is found. This is
    the only barrier between Phase 2 and accidental real-money trades —
    keep it inviolable.

    Lazy import of `futu` so the test suite (which uses FakePaperTrader)
    doesn't pay the SDK startup cost.
    """

    def __init__(
        self, host: str = "127.0.0.1", port: int = 11111, market: str = "US"
    ) -> None:
        from futu import (  # type: ignore[import-not-found]
            OpenSecTradeContext,
            SecurityFirm,
            TrdEnv,
            TrdMarket,
        )

        self._TrdEnv = TrdEnv
        market_enum = getattr(TrdMarket, market.upper(), TrdMarket.US)
        self._ctx = OpenSecTradeContext(
            filter_trdmarket=market_enum,
            host=host,
            port=port,
            security_firm=SecurityFirm.FUTUSECURITIES,
        )

        ret, data = self._ctx.get_acc_list()
        if ret != 0:
            raise PaperTradeError(f"get_acc_list failed: {data}")
        sim_accs = data[data["trd_env"] == TrdEnv.SIMULATE]
        if sim_accs.empty:
            raise PaperTradeError(
                "no SIMULATE account in OpenD — log in to a paper account"
            )
        self._acc_id = int(sim_accs.iloc[0]["acc_id"])

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8), reraise=True)
    def place_order(
        self,
        *,
        code: str,
        side: OrderSide,
        qty: int,
        order_type: OrderType = "MARKET",
        limit_price: float | None = None,
    ) -> PaperOrderResult:
        # futu SDK renamed `OrderSide` → `TrdSide` in newer releases; use
        # `TrdSide` and keep our internal `OrderSide` literal for typing.
        from futu import (  # type: ignore[import-not-found]
            OrderType as FOrderType,
            TrdSide,
        )

        side_enum = TrdSide.BUY if side == "BUY" else TrdSide.SELL
        type_enum = (
            FOrderType.MARKET if order_type == "MARKET" else FOrderType.NORMAL
        )
        price = 0.0 if order_type == "MARKET" else float(limit_price or 0.0)

        ret, data = self._ctx.place_order(
            price=price,
            qty=int(qty),
            code=code,
            trd_side=side_enum,
            order_type=type_enum,
            trd_env=self._TrdEnv.SIMULATE,
            acc_id=self._acc_id,
        )
        now = datetime.now(tz=timezone.utc)
        if ret != 0:
            return PaperOrderResult(
                order_id="",
                status="REJECTED",
                code=code,
                side=side,
                requested_qty=qty,
                filled_qty=0,
                avg_fill_price=0.0,
                submitted_at=now,
                error=str(data),
            )
        row = data.iloc[0]
        # Futu order status strings: SUBMITTED, FILLED_PART, FILLED_ALL, ...
        raw_status = str(row.get("order_status", ""))
        status: OrderStatus = (
            "FILLED" if raw_status == "FILLED_ALL" else "PENDING"
        )
        return PaperOrderResult(
            order_id=str(row["order_id"]),
            status=status,
            code=code,
            side=side,
            requested_qty=qty,
            filled_qty=int(row.get("dealt_qty", 0)),
            avg_fill_price=float(row.get("dealt_avg_price", 0.0)),
            submitted_at=now,
        )

    def cancel_order(self, order_id: str) -> None:
        from futu import ModifyOrderOp  # type: ignore[import-not-found]

        ret, data = self._ctx.modify_order(
            modify_order_op=ModifyOrderOp.CANCEL,
            order_id=order_id,
            qty=0,
            price=0,
            trd_env=self._TrdEnv.SIMULATE,
            acc_id=self._acc_id,
        )
        if ret != 0:
            raise PaperTradeError(f"cancel_order failed: {data}")

    def query_positions(self) -> list[PaperPosition]:
        ret, data = self._ctx.position_list_query(
            trd_env=self._TrdEnv.SIMULATE, acc_id=self._acc_id
        )
        if ret != 0:
            raise PaperTradeError(f"position_list_query failed: {data}")
        out: list[PaperPosition] = []
        for _, row in data.iterrows():
            qty = int(row.get("qty", 0))
            if qty == 0:
                continue
            out.append(
                PaperPosition(
                    code=str(row["code"]),
                    qty=qty,
                    avg_cost=float(row.get("cost_price", 0.0)),
                    market_value=float(row["market_val"]) if "market_val" in row else None,
                    unrealized_pnl=float(row["pl_val"]) if "pl_val" in row else None,
                )
            )
        return out

    def query_today_orders(self) -> list[PaperOrder]:
        ret, data = self._ctx.order_list_query(
            trd_env=self._TrdEnv.SIMULATE, acc_id=self._acc_id
        )
        if ret != 0:
            raise PaperTradeError(f"order_list_query failed: {data}")
        out: list[PaperOrder] = []
        for _, row in data.iterrows():
            raw_status = str(row.get("order_status", ""))
            status: OrderStatus = "FILLED" if raw_status == "FILLED_ALL" else (
                "CANCELLED" if raw_status == "CANCELLED_ALL" else "PENDING"
            )
            side: OrderSide = "BUY" if str(row.get("trd_side", "")) == "BUY" else "SELL"
            out.append(
                PaperOrder(
                    order_id=str(row["order_id"]),
                    code=str(row["code"]),
                    side=side,
                    qty=int(row.get("qty", 0)),
                    price=float(row["price"]) if row.get("price") else None,
                    status=status,
                    submitted_at=_parse_futu_ts(str(row.get("create_time", ""))),
                    filled_qty=int(row.get("dealt_qty", 0)),
                    avg_fill_price=float(row.get("dealt_avg_price", 0.0)),
                )
            )
        return out

    def close(self) -> None:
        try:
            self._ctx.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _new_oid() -> str:
    return "po_" + uuid.uuid4().hex[:16]


def _parse_futu_ts(s: str) -> datetime:
    if not s:
        return datetime.now(tz=timezone.utc)
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.now(tz=timezone.utc)
