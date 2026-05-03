from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from tenacity import retry, stop_after_attempt, wait_exponential


FREQ_TO_KTYPE: dict[str, str] = {
    "1m": "K_1M",
    "5m": "K_5M",
    "15m": "K_15M",
    "30m": "K_30M",
    "60m": "K_60M",
    "D": "K_DAY",
    "W": "K_WEEK",
}


@dataclass(frozen=True, slots=True)
class Snapshot:
    code: str
    last_price: float
    open_price: float
    high_price: float
    low_price: float
    volume: int
    turnover: float
    update_time: datetime


@dataclass(frozen=True, slots=True)
class Candle:
    code: str
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    turnover: float


class FutuClient(Protocol):
    def snapshot(self, codes: Sequence[str]) -> list[Snapshot]: ...
    def kline(
        self,
        code: str,
        *,
        ktype: str,
        limit: int,
    ) -> list[Candle]: ...
    def close(self) -> None: ...


class OpenDClient:
    """Real client backed by futu-api OpenQuoteContext."""

    def __init__(self, host: str = "127.0.0.1", port: int = 11111) -> None:
        from futu import OpenQuoteContext

        self._ctx = OpenQuoteContext(host=host, port=port)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    def snapshot(self, codes: Sequence[str]) -> list[Snapshot]:
        from futu import RET_OK

        ret, df = self._ctx.get_market_snapshot(list(codes))
        if ret != RET_OK:
            raise RuntimeError(f"snapshot failed: {df}")
        out: list[Snapshot] = []
        for _, row in df.iterrows():
            out.append(
                Snapshot(
                    code=row["code"],
                    last_price=float(row["last_price"]),
                    open_price=float(row["open_price"]),
                    high_price=float(row["high_price"]),
                    low_price=float(row["low_price"]),
                    volume=int(row["volume"]),
                    turnover=float(row["turnover"]),
                    update_time=datetime.fromisoformat(str(row["update_time"])),
                )
            )
        return out

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    def kline(self, code: str, *, ktype: str, limit: int) -> list[Candle]:
        """Fetch the *most recent* `limit` bars of the given ktype.

        IMPORTANT: when no `start`/`end` is passed, OpenD returns the EARLIEST
        bars from the user's quote-permission window — not the most recent.
        We always pass an explicit `end=today` so we get the latest bars.
        """
        from datetime import datetime, timedelta

        from futu import KLType, RET_OK

        kt = {
            "K_1M": KLType.K_1M,
            "K_5M": KLType.K_5M,
            "K_15M": KLType.K_15M,
            "K_30M": KLType.K_30M,
            "K_60M": KLType.K_60M,
            "K_DAY": KLType.K_DAY,
            "K_WEEK": KLType.K_WEEK,
        }[ktype]
        end = datetime.now().strftime("%Y-%m-%d")
        # How many calendar days to pull so OpenD will return at least `limit` bars.
        if ktype in {"K_1M", "K_5M", "K_15M", "K_30M"}:
            lookback_days = max(20, limit // 60 + 1)  # tight intraday window
        elif ktype == "K_60M":
            lookback_days = max(60, limit)  # ~6.5h trading * 5 trading days/wk
        elif ktype == "K_WEEK":
            lookback_days = max(180, limit * 7)  # ~52 weeks ≈ 1 year
        else:  # K_DAY (and any unhandled future ktype)
            lookback_days = max(30, limit * 2)
        start = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
        ret, df, _ = self._ctx.request_history_kline(
            code, ktype=kt, start=start, end=end, max_count=limit
        )
        if ret != RET_OK:
            raise RuntimeError(f"kline failed: {df}")
        out: list[Candle] = []
        for _, row in df.iterrows():
            out.append(
                Candle(
                    code=code,
                    ts=datetime.fromisoformat(str(row["time_key"])),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=int(row["volume"]),
                    turnover=float(row["turnover"]),
                )
            )
        return out

    def close(self) -> None:
        self._ctx.close()


class FakeFutuClient:
    """In-memory fake for tests. Caller pre-loads snapshots / candles."""

    def __init__(self) -> None:
        self._snapshots: dict[str, Snapshot] = {}
        self._klines: dict[tuple[str, str], list[Candle]] = {}
        self.closed = False

    def set_snapshot(self, snap: Snapshot) -> None:
        self._snapshots[snap.code] = snap

    def set_kline(self, code: str, ktype: str, candles: list[Candle]) -> None:
        self._klines[(code, ktype)] = list(candles)

    def snapshot(self, codes: Sequence[str]) -> list[Snapshot]:
        return [self._snapshots[c] for c in codes if c in self._snapshots]

    def kline(self, code: str, *, ktype: str, limit: int) -> list[Candle]:
        return self._klines.get((code, ktype), [])[-limit:]

    def close(self) -> None:
        self.closed = True
