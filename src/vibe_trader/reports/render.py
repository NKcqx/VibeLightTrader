from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from datetime import datetime
from importlib.resources import files
from typing import Any
from zoneinfo import ZoneInfo

from jinja2 import Environment

from dataclasses import dataclass

from vibe_trader.reports.card import SEVERITY_COLOR, SEVERITY_EMOJI
from vibe_trader.signals.base import Severity, Signal


_TZ_ET = ZoneInfo("America/New_York")
_TZ_CN = ZoneInfo("Asia/Shanghai")
_SEVERITY_RANK = {Severity.INFO: 0, Severity.WARN: 1, Severity.CRITICAL: 2}


def _load_template(name: str) -> str:
    pkg = files("vibe_trader.reports") / "templates"
    return (pkg / name).read_text()


def _env() -> Environment:
    return Environment(autoescape=False)


def _ts_str(ts: datetime) -> str:
    et = ts.astimezone(_TZ_ET).strftime("%Y-%m-%d %H:%M ET")
    cn = ts.astimezone(_TZ_CN).strftime("%Y-%m-%d %H:%M +8")
    return f"{et} ({cn})"


_SIGNAL_NAME = {
    "rsi_overbought": "RSI 超买",
    "rsi_oversold": "RSI 超卖",
    "macd_golden_cross": "MACD 金叉",
    "macd_death_cross": "MACD 死叉",
    "boll_upper_break": "突破布林上轨",
    "boll_lower_break": "跌破布林下轨",
    "threshold_breach_upper": "穿越上限阈值",
    "threshold_breach_lower": "穿越下限阈值",
}


# Each entry: (header_template, meaning_string).
# `header_template` may use {close}, {upper}, {lower}, {rsi}, {boll_upper},
# {boll_lower} placeholders; missing payload keys render as "n/a".
# Keep meanings ≤ ~60 字 — long lines wrap awkwardly in Lark cards.
_SIGNAL_EXPLAIN: dict[str, tuple[str, str]] = {
    "threshold_breach_upper": (
        "现价 {close} 已突破你设的卖出预警线 {upper}",
        "按你的阈值规则进入止盈/减仓窗口；技术面通常也视作超买信号",
    ),
    "threshold_breach_lower": (
        "现价 {close} 已跌破你设的买入观察线 {lower}",
        "按你的阈值规则进入加仓/抄底窗口；技术面通常也视作超跌信号",
    ),
    "rsi_overbought": (
        "RSI {rsi} 高于 70",
        "短线动能可能见顶，回调风险上升（>70 常被视作超买区间）",
    ),
    "rsi_oversold": (
        "RSI {rsi} 低于 30",
        "短线动能可能见底，反弹机会上升（<30 常被视作超卖区间）",
    ),
    "macd_golden_cross": (
        "MACD 上穿信号线（金叉）",
        "中短期动量由空转多，常被视作初步多头信号",
    ),
    "macd_death_cross": (
        "MACD 下穿信号线（死叉）",
        "中短期动量由多转空，常被视作初步空头信号",
    ),
    "boll_upper_break": (
        "收盘 {close} 突破布林带上轨 {boll_upper}",
        "价格偏离 20 周期均值约 +2σ，处于统计意义上的高位区间",
    ),
    "boll_lower_break": (
        "收盘 {close} 跌破布林带下轨 {boll_lower}",
        "价格偏离 20 周期均值约 -2σ，处于统计意义上的低位区间",
    ),
}


def _fmt_price(v: Any) -> str:
    """`$198.45` if numeric, else `n/a`."""
    try:
        return f"${float(v):.2f}"
    except (TypeError, ValueError):
        return "n/a"


def _fmt_num(v: Any, *, decimals: int = 2) -> str:
    """Plain decimal — for RSI / MACD / Bollinger band values that aren't prices."""
    try:
        return f"{float(v):.{decimals}f}"
    except (TypeError, ValueError):
        return "n/a"


def explain_signal(s: Signal) -> str:
    """Two-line markdown explanation of one signal:

        **<中文名>** — <关键数字组合>
          ↳ <一句话含义>

    For unknown signal_type or missing payload keys we degrade gracefully
    — `(详情: k=v, ...)` fallback so we never lose information.

    Pure / deterministic: no clock, no env, no IO. Trivially unit-testable.
    """
    name = _SIGNAL_NAME.get(s.signal_type, s.signal_type)
    spec = _SIGNAL_EXPLAIN.get(s.signal_type)

    if spec is None:
        detail = ", ".join(f"{k}={v}" for k, v in s.payload.items())
        return f"**{name}** ({detail})" if detail else f"**{name}**"

    header_tpl, meaning = spec
    p = s.payload
    fields = {
        "close": _fmt_price(p.get("close")),
        "upper": _fmt_price(p.get("upper")),
        "lower": _fmt_price(p.get("lower")),
        "boll_upper": _fmt_price(p.get("boll_upper")),
        "boll_lower": _fmt_price(p.get("boll_lower")),
        "rsi": _fmt_num(p.get("rsi"), decimals=2),
    }
    try:
        header = header_tpl.format(**fields)
    except KeyError:  # pragma: no cover — defensive: missing template var
        header = header_tpl

    return f"**{name}** — {header}\n  ↳ {meaning}"


def _signal_line(s: Signal) -> str:
    """Backward-compat wrapper preserved for any external callers."""
    return explain_signal(s)


def render_signal_alert(
    *,
    code: str,
    ts: datetime,
    close: float,
    change_pct: float,
    signals: Sequence[Signal],
    signal_ids: Sequence[int] = (),
    suggestion: dict[str, Any] | None = None,
    diagnostics_md: str = "",
) -> dict[str, Any]:
    """Render a signal-alert Lark Interactive Card to a dict (ready for JSON).

    `suggestion` (optional, P2): {'action', 'qty', 'reason', 'signal_id'}
    appended as a "建议动作" section with copy-paste confirm command.
    """
    severity = max(
        (s.severity for s in signals),
        key=lambda x: _SEVERITY_RANK[x],
        default=Severity.INFO,
    )
    # Empty line between bullets — pairs of (feature line, meaning line)
    # become much easier to read with a paragraph break between signals.
    signals_md = "\n\n".join(f"• {explain_signal(s)}" for s in signals)
    change_str = f"{'▲' if change_pct >= 0 else '▼'} {change_pct:+.2%}"

    suggestion_md = ""
    if suggestion:
        action = suggestion.get("action", "HOLD")
        qty = suggestion.get("qty", 0)
        reason = suggestion.get("reason", "")
        sig_id = suggestion.get("signal_id")
        if action == "HOLD":
            suggestion_md = f"**📋 建议:** 观望  \n_{reason}_"
        else:
            cmd = (
                f"`vibe-trader trade confirm {sig_id}`"
                if sig_id is not None
                else ""
            )
            suggestion_md = (
                f"**📋 建议:** {action} {qty} 股  \n"
                f"_{reason}_  \n"
                + (f"确认下单: {cmd}" if cmd else "")
            )
    elif signal_ids:
        # No suggestion but expose signal_ids so user can locate via `trade list`
        sid_str = ", ".join(f"#{sid}" for sid in signal_ids)
        suggestion_md = f"_signal_id: {sid_str}_"

    tpl = _env().from_string(_load_template("signal_alert.json.j2"))
    rendered = tpl.render(
        code=code,
        severity=severity.value,
        color=SEVERITY_COLOR[severity],
        emoji=SEVERITY_EMOJI[severity],
        close=close,
        change_str=change_str,
        signals_md=signals_md,
        suggestion_md=suggestion_md,
        diagnostics_md=diagnostics_md,
        ts_str=_ts_str(ts),
    )
    return json.loads(rendered)


def interpret_indicators(
    *,
    close: float | None,
    rsi: float | None = None,
    macd: float | None = None,
    macd_signal: float | None = None,
    macd_hist: float | None = None,
    boll_upper: float | None = None,
    boll_mid: float | None = None,
    boll_lower: float | None = None,
) -> str:
    """Produce a one-line Chinese interpretation of indicator state.

    Empty string if no values are available (caller decides whether to omit).
    """
    parts: list[str] = []
    if rsi is not None:
        if rsi >= 70:
            parts.append(f"RSI {rsi:.0f} (超买)")
        elif rsi >= 60:
            parts.append(f"RSI {rsi:.0f} (偏强)")
        elif rsi <= 30:
            parts.append(f"RSI {rsi:.0f} (超卖)")
        elif rsi <= 40:
            parts.append(f"RSI {rsi:.0f} (偏弱)")
        else:
            parts.append(f"RSI {rsi:.0f} (中性)")
    if macd is not None and macd_signal is not None:
        bias = "多头" if macd > macd_signal else "空头"
        cross = ""
        if macd_hist is not None:
            if macd_hist > 0 and macd_hist > 0.05:
                cross = " · 柱线扩张"
            elif macd_hist < 0 and macd_hist < -0.05:
                cross = " · 空头扩张"
        parts.append(f"MACD {bias}{cross}")
    if (
        close is not None
        and boll_upper is not None
        and boll_mid is not None
        and boll_lower is not None
    ):
        if close > boll_upper:
            parts.append("BOLL 突破上轨")
        elif close < boll_lower:
            parts.append("BOLL 跌破下轨")
        elif close > boll_mid:
            parts.append("BOLL 中轨上方")
        else:
            parts.append("BOLL 中轨下方")
    return " · ".join(parts)


def render_daily_brief(
    *,
    kind: str,
    date_str: str,
    rows: Iterable[dict[str, Any]],
    summary_lines: Sequence[str] = (),
    pnl_lines: Sequence[str] = (),
) -> dict[str, Any]:
    """Render the morning/closing brief card.

    Each row dict supports the following optional fields beyond the basics:
        analysis (str): indicator interpretation line
        pnl_str  (str): per-symbol position + unrealized P&L summary

    `pnl_lines` (P2 aggregate): if non-empty, appended as a "纸面盘 P&L" section.
    """
    rows_md_lines: list[str] = []
    for r in rows:
        change_arrow = "▲" if r["change_pct"] >= 0 else "▼"
        head = (
            f"**[{r['code']}]** **${r['close']:.2f}**  "
            f"{change_arrow} {r['change_pct']:+.2%}  信号:{r.get('signal_count', 0)}"
        )
        rows_md_lines.append(head)
        if r.get("analysis"):
            rows_md_lines.append(f"  📊 {r['analysis']}")
        if r.get("pnl_str"):
            rows_md_lines.append(f"  💰 {r['pnl_str']}")
    rows_md = "\n".join(rows_md_lines)
    summary_md = "\n".join(f"• {line}" for line in summary_lines)
    pnl_md = ""
    if pnl_lines:
        pnl_md = "**📈 纸面盘 P&L:**\n" + "\n".join(f"• {line}" for line in pnl_lines)

    tpl = _env().from_string(_load_template("daily_brief.json.j2"))
    rendered = tpl.render(
        kind=kind,
        date_str=date_str,
        rows_md=rows_md,
        summary_md=summary_md,
        pnl_md=pnl_md,
    )
    return json.loads(rendered)


@dataclass(frozen=True)
class WatchlistCardRow:
    """Pre-composed body markdown for one symbol on the watchlist card."""

    code: str
    body_md: str


def render_watchlist_card(
    *,
    title: str,
    action_text: str,
    rows: Sequence[WatchlistCardRow],
    ts: datetime,
    color: str = "blue",
    footer_md: str = "",
) -> dict[str, Any]:
    """Render a Lark card for /list, /add, /remove, /threshold replies.

    `action_text` is the operation summary (e.g. "✅ 已添加 US.AAPL ...").
    `rows` is the current full watchlist with per-symbol diagnostic markdown.
    """
    tpl = _env().from_string(_load_template("watchlist_card.json.j2"))
    rendered = tpl.render(
        title=title,
        color=color,
        action_text=action_text,
        rows=rows,
        footer_md=footer_md,
        ts_str=_ts_str(ts),
    )
    return json.loads(rendered)
