"""Human-readable interpretation of indicator and position state.

These pure functions translate raw numeric indicator readings into Chinese
phrases the Lark card can show — e.g. "RSI 25.3 超卖", "MACD hist +0.42 金叉".
Keep them deterministic (no side effects, no network) so they're trivial to
unit-test.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class IndicatorReading:
    """Compact summary of the latest indicator row for one symbol."""

    rsi_14: float | None
    macd: float | None
    macd_signal: float | None
    macd_hist: float | None
    boll_upper: float | None
    boll_mid: float | None
    boll_lower: float | None
    close: float

    def lines(
        self,
        *,
        rsi_overbought: float = 70.0,
        rsi_oversold: float = 30.0,
    ) -> list[str]:
        """Render into bullet-list strings ready for `signals_md` injection."""
        out: list[str] = []
        if self.rsi_14 is not None:
            status = (
                "超买区间"
                if self.rsi_14 > rsi_overbought
                else "超卖区间"
                if self.rsi_14 < rsi_oversold
                else "中性区间"
            )
            out.append(f"RSI(14) **{self.rsi_14:.1f}** · {status}")
        if (
            self.macd is not None
            and self.macd_signal is not None
            and self.macd_hist is not None
        ):
            cross = (
                "金叉态势" if self.macd > self.macd_signal else "死叉态势"
            )
            sign = "+" if self.macd_hist >= 0 else ""
            out.append(
                f"MACD **{self.macd:+.3f}** / Signal {self.macd_signal:+.3f} "
                f"· hist {sign}{self.macd_hist:.3f} · {cross}"
            )
        if self.boll_upper is not None and self.boll_lower is not None:
            if self.close > self.boll_upper:
                pos = f"突破上轨 (+{(self.close / self.boll_upper - 1) * 100:.1f}%)"
            elif self.close < self.boll_lower:
                pos = f"跌破下轨 ({(self.close / self.boll_lower - 1) * 100:+.1f}%)"
            else:
                width = self.boll_upper - self.boll_lower
                pct_in_band = (
                    (self.close - self.boll_lower) / width * 100
                    if width > 0
                    else 50.0
                )
                pos = f"通道内 ({pct_in_band:.0f}% 位置)"
            out.append(
                f"BOLL [{self.boll_lower:.2f} / {self.boll_mid:.2f} / "
                f"{self.boll_upper:.2f}] · {pos}"
            )
        return out


@dataclass(frozen=True)
class ReturnSummary:
    """Multi-horizon return %s. Each is a fraction (0.025 = 2.5%)."""

    intraday: float | None  # last vs today's open
    last_30_bars: float | None  # close vs close 30 trading-hours ago

    def line(self) -> str:
        parts: list[str] = []
        if self.intraday is not None:
            arrow = "▲" if self.intraday >= 0 else "▼"
            parts.append(f"日内 {arrow} {self.intraday:+.2%}")
        if self.last_30_bars is not None:
            arrow = "▲" if self.last_30_bars >= 0 else "▼"
            parts.append(f"近 30 根 {arrow} {self.last_30_bars:+.2%}")
        return " · ".join(parts)


@dataclass(frozen=True)
class PositionSummary:
    """Current paper position for one symbol."""

    qty: int
    avg_cost: float
    mark: float

    @property
    def market_value(self) -> float:
        return self.qty * self.mark

    @property
    def unrealized_pnl(self) -> float:
        return (self.mark - self.avg_cost) * self.qty

    @property
    def return_pct(self) -> float:
        if self.avg_cost <= 0:
            return 0.0
        return (self.mark - self.avg_cost) / self.avg_cost

    def line(self) -> str:
        sign = "+" if self.unrealized_pnl >= 0 else ""
        return (
            f"持仓 **{self.qty}** 股 @ ${self.avg_cost:.2f}  "
            f"市值 ${self.market_value:,.0f}  "
            f"浮盈 {sign}${self.unrealized_pnl:,.0f} ({self.return_pct:+.2%})"
        )


def build_diagnostics_md(
    *,
    indicator: IndicatorReading | None,
    returns: ReturnSummary | None,
    position: PositionSummary | None,
) -> str:
    """Compose all three blocks into a single markdown body for cards.

    Returns empty string if every block is None — caller can suppress section.
    """
    parts: list[str] = []
    if returns is not None:
        line = returns.line()
        if line:
            parts.append(f"**📈 走势:** {line}")
    if indicator is not None:
        ind_lines = indicator.lines()
        if ind_lines:
            parts.append("**📊 技术面:**\n" + "\n".join(f"• {ln}" for ln in ind_lines))
    if position is not None:
        parts.append(f"**💼 {position.line()}**")
    return "\n\n".join(parts)


def reading_from_row(row: dict[str, Any], *, close: float) -> IndicatorReading:
    """Build an IndicatorReading from a pandas row-like dict."""
    return IndicatorReading(
        rsi_14=_f(row.get("rsi_14")),
        macd=_f(row.get("macd")),
        macd_signal=_f(row.get("macd_signal")),
        macd_hist=_f(row.get("macd_hist")),
        boll_upper=_f(row.get("boll_upper")),
        boll_mid=_f(row.get("boll_mid")),
        boll_lower=_f(row.get("boll_lower")),
        close=close,
    )


def _f(v: Any) -> float | None:
    """NaN/None-safe float coercion."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN check
        return None
    return f
