from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from datetime import datetime
from importlib.resources import files
from typing import Any
from zoneinfo import ZoneInfo

from jinja2 import Environment

from equity_monitor.reports.card import SEVERITY_COLOR, SEVERITY_EMOJI
from equity_monitor.signals.base import Severity, Signal


_TZ_ET = ZoneInfo("America/New_York")
_TZ_CN = ZoneInfo("Asia/Shanghai")
_SEVERITY_RANK = {Severity.INFO: 0, Severity.WARN: 1, Severity.CRITICAL: 2}


def _load_template(name: str) -> str:
    pkg = files("equity_monitor.reports") / "templates"
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
    "futu_tech_anomaly": "技术异动",
    "futu_capital_anomaly": "资金异动",
    "news_negative_burst": "负面舆情突增",
    "news_positive_burst": "正面舆情突增",
}


def _signal_line(s: Signal) -> str:
    name = _SIGNAL_NAME.get(s.signal_type, s.signal_type)
    detail = ", ".join(f"{k}={v}" for k, v in s.payload.items())
    return f"{name} ({detail})" if detail else name


def render_signal_alert(
    *,
    code: str,
    ts: datetime,
    close: float,
    change_pct: float,
    signals: Sequence[Signal],
    news_titles: Sequence[str] = (),
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
    signals_md = "\n".join(f"• {_signal_line(s)}" for s in signals)
    news_md = "\n".join(f"• {t}" for t in news_titles)
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
                f"`equity-monitor trade confirm {sig_id}`"
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
        news_md=news_md,
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
        sentiment (str): one-line sentiment note (skipped if absent)

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
        if r.get("sentiment"):
            rows_md_lines.append(f"  💬 {r['sentiment']}")
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


def render_news_pulse(
    *,
    code: str,
    direction: str,
    temp_now: float,
    temp_prev: float,
    news_titles: Sequence[str],
) -> dict[str, Any]:
    headline = "负面舆情突增" if direction == "negative" else "正面舆情突增"
    color = "red" if direction == "negative" else "green"
    news_md = "\n".join(f"• {t}" for t in news_titles)

    tpl = _env().from_string(_load_template("news_pulse.json.j2"))
    rendered = tpl.render(
        code=code,
        headline=headline,
        color=color,
        temp_now=f"{temp_now:.1f}",
        temp_prev=f"{temp_prev:.1f}",
        news_md=news_md,
    )
    return json.loads(rendered)
