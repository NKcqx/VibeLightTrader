"""Markdown rendering for the per-symbol journal.

Pure functions: dataclass in, string out. No file I/O, no logging.
Easy to unit-test against fixed input → expected text.

Conventions:

- Section markers are HTML comments so they don't show up in rendered
  Markdown but are unambiguous to a regex on read-back.
- Action emojis: 🟢 BUY / 🔴 SELL / 🟡 HOLD / ⚪ no-signal-tick.
  Used both in the tick title and in the overview "最近一次决策" line.
- Indicator and price formatting is locale-neutral (no thousands
  separators) so diffs across days stay clean.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from equity_monitor.signals.base import Signal
from equity_monitor.signals.strategy_lite import SignalSuggest

# Markers used by the writer to slice/replace the overview block.
OVERVIEW_BEGIN = "<!-- overview-start -->"
OVERVIEW_END = "<!-- overview-end -->"

# Markers around every event entry. Used so we can confidently
# locate "where the first entry begins" when prepending a new one.
EVENT_DELIMITER = "---"


def action_emoji(action: str | None) -> str:
    """Return a single-glyph indicator for the action.

    `None` collapses to ⚪ (a tick that produced no decision — usually
    "no signals fired"). HOLD is yellow because it IS a decision (the
    LLM looked and chose not to act); we want it visually distinct
    from "nothing happened".
    """
    return {
        "BUY": "🟢",
        "SELL": "🔴",
        "HOLD": "🟡",
        None: "⚪",
    }.get(action, "⚪")


# ---------------------------------------------------------------------------
# Overview block
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OverviewSnapshot:
    """All fields rendered into the per-symbol overview block.

    None-able fields render as "—" so the block survives missing data
    (e.g. before the first signal). The writer composes this object
    every time it touches the file.
    """

    code: str
    display_name: str | None  # "NVIDIA"; falls back to code if None.

    last_check_ts: datetime  # always-set: the latest run that touched the file
    last_price: float | None  # most recent snapshot price
    intraday_pct: float | None  # last_price vs day-open

    upper_threshold: float | None
    lower_threshold: float | None

    position_qty: int  # 0 if flat
    avg_cost: float | None  # None when flat
    unrealized_pnl: float | None  # None when flat or no last_price

    total_events: int  # cumulative count of entries below
    counts_by_action: dict[str, int]  # {"BUY": 3, "SELL": 1, "HOLD": 8}
    fallback_count: int  # how many events used a fallback path

    last_decision_action: str | None  # most recent BUY/SELL/HOLD; None if no events
    last_decision_ts: datetime | None
    last_decision_client: str | None  # 'cursor-agent:default', 'rule', etc.
    last_decision_confidence: float | None

    # Optional decoration. Empty list / None → section is omitted. Lets
    # the writer pass these through without compute_overview() needing
    # to know about metrics/errors modules (avoids a circular import).
    hit_rate_lines: tuple[str, ...] = ()
    """Already-formatted bullet strings, one per window. Caller produces
    these via `metrics.render_hit_rate_lines(...)`."""

    error_probe_lines: tuple[str, ...] = ()
    """Already-formatted bullet strings; empty when the symbol is
    healthy. Caller produces these via `errors.render_probe_lines(...)`."""


def _fmt_price(p: float | None) -> str:
    return f"${p:.2f}" if p is not None else "—"


def _fmt_pct(p: float | None) -> str:
    if p is None:
        return "—"
    sign = "+" if p >= 0 else ""
    return f"{sign}{p * 100:.2f}%"


def _fmt_pnl(amt: float | None) -> str:
    if amt is None:
        return "—"
    sign = "+" if amt >= 0 else ""
    return f"{sign}${amt:.2f}"


def render_overview(ov: OverviewSnapshot) -> str:
    """Render the overview block, including the start/end markers.

    Always returns a complete block ending with EOL; callers paste it
    in directly between markers.
    """
    name = ov.display_name or ov.code

    threshold_line = (
        f"${ov.lower_threshold:.0f} ~ ${ov.upper_threshold:.0f}"
        if ov.lower_threshold is not None and ov.upper_threshold is not None
        else "—"
    )

    if ov.position_qty > 0:
        held_line = (
            f"{ov.position_qty} 股"
            + (f" @ {_fmt_price(ov.avg_cost)}" if ov.avg_cost is not None else "")
            + (f" → 浮盈 {_fmt_pnl(ov.unrealized_pnl)}" if ov.unrealized_pnl is not None else "")
        )
    else:
        held_line = "无持仓"

    counts = ov.counts_by_action
    counts_line = (
        f"{ov.total_events} 次 · "
        f"{counts.get('BUY', 0)} BUY / "
        f"{counts.get('SELL', 0)} SELL / "
        f"{counts.get('HOLD', 0)} HOLD"
    )
    if ov.fallback_count > 0:
        counts_line += f" · ⚠️ {ov.fallback_count} 次 fallback"

    if ov.last_decision_action is not None:
        decision_meta_parts: list[str] = []
        if ov.last_decision_client:
            decision_meta_parts.append(ov.last_decision_client)
        if ov.last_decision_confidence is not None:
            decision_meta_parts.append(f"conf {ov.last_decision_confidence:.2f}")
        meta_str = f"（{'，'.join(decision_meta_parts)}）" if decision_meta_parts else ""
        ts_str = (
            f" · {_fmt_ts(ov.last_decision_ts)}"
            if ov.last_decision_ts is not None
            else ""
        )
        last_decision_line = (
            f"{action_emoji(ov.last_decision_action)} "
            f"**{ov.last_decision_action}**{meta_str}{ts_str}"
        )
    else:
        last_decision_line = "—（尚无决策事件）"

    lines: list[str] = [
        f"# {ov.code} · {name} 监控日志",
        "",
        OVERVIEW_BEGIN,
        "## 当前状态",
        "",
        f"- **最新价**：{_fmt_price(ov.last_price)}（{_fmt_ts(ov.last_check_ts)}，日内 {_fmt_pct(ov.intraday_pct)}）",
        f"- **持仓**：{held_line}",
        f"- **阈值区间**：{threshold_line}",
        f"- **累计事件**：{counts_line}",
        f"- **最近决策**：{last_decision_line}",
    ]

    if ov.hit_rate_lines:
        lines.append("")
        lines.append("**决策胜率**")
        lines.extend(ov.hit_rate_lines)
    if ov.error_probe_lines:
        lines.append("")
        lines.extend(ov.error_probe_lines)

    lines.append(OVERVIEW_END)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Event entry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JournalEntry:
    """One event to be appended to the journal.

    `signals` is the deduped list that fired this tick; for a
    no-signal tick the writer takes the refresh_overview_only path
    instead and never builds a JournalEntry.
    """

    code: str
    ts: datetime  # event timestamp (US/Eastern in the rendered text)

    last_price: float | None
    intraday_pct: float | None
    last_30_bar_pct: float | None

    rsi_14: float | None
    macd: float | None
    macd_signal: float | None
    macd_hist: float | None
    boll_upper: float | None
    boll_mid: float | None
    boll_lower: float | None

    position_qty: int
    avg_cost: float | None
    unrealized_pnl: float | None

    signals: Sequence[Signal]
    suggestion: SignalSuggest | None  # None when strategy returned no opinion

    audit_log_path: str | None  # path string for the "审计参考" line; None hides it
    chart_image_path: str | None  # local PNG path; None hides the image link


def _fmt_ts(ts: datetime | None) -> str:
    if ts is None:
        return "—"
    # US/Eastern label — caller is responsible for tz conversion;
    # we just print whatever we got and append "ET" iff tz info
    # carries an Eastern offset OR the caller already converted.
    return ts.strftime("%Y-%m-%d %H:%M %Z").rstrip()


def _signal_explainer_line(sig: Signal) -> str:
    """One-line rendering: `signal_type` — payload digest.

    Mirrors the Lark card's signal explainer style (short, terminologically
    precise). Falls back to a generic "信号触发" if we don't have a
    template for this signal_type.
    """
    payload = sig.payload or {}
    pretty: list[str] = []
    for k in ("close", "upper", "lower"):
        if k in payload:
            v = payload[k]
            pretty.append(f"{k}={v}" if not isinstance(v, float) else f"{k}={v:.2f}")
    for k in ("rsi", "macd_hist"):
        if k in payload:
            v = payload[k]
            pretty.append(f"{k}={v}" if not isinstance(v, float) else f"{k}={v:.2f}")
    digest = ", ".join(pretty) if pretty else ""
    suffix = f" — {digest}" if digest else ""
    return f"- `{sig.signal_type}` ({sig.severity.value}){suffix}"


def render_event(entry: JournalEntry) -> str:
    """Render one event entry. Caller wraps it with a leading `---` line."""
    sug = entry.suggestion
    action = sug.action if sug is not None else None
    title_action = sug.action if sug is not None else "无决策"
    qty_part = f" {sug.qty}" if (sug is not None and sug.action != "HOLD" and sug.qty) else ""

    header = (
        f"## {_fmt_ts(entry.ts)} — {action_emoji(action)} {title_action}{qty_part}"
    )

    sig_lines = "\n".join(_signal_explainer_line(s) for s in entry.signals)

    rows: list[tuple[str, str]] = []
    rows.append(
        (
            "最新价",
            f"{_fmt_price(entry.last_price)}"
            f"（日内 {_fmt_pct(entry.intraday_pct)}, 30K线 {_fmt_pct(entry.last_30_bar_pct)}）",
        )
    )
    if entry.rsi_14 is not None:
        rows.append(("RSI(14)", f"{entry.rsi_14:.2f}"))
    if entry.macd_hist is not None:
        macd_label = (
            f"{entry.macd_hist:+.2f}"
            + (
                "（多头）"
                if entry.macd_hist > 0
                else "（空头）"
                if entry.macd_hist < 0
                else "（中性）"
            )
        )
        rows.append(("MACD hist", macd_label))
    if entry.boll_upper is not None and entry.boll_lower is not None:
        rows.append(
            (
                "布林带",
                f"上 {_fmt_price(entry.boll_upper)} / 中 {_fmt_price(entry.boll_mid)} / 下 {_fmt_price(entry.boll_lower)}",
            )
        )
    if entry.position_qty > 0:
        pos = (
            f"{entry.position_qty} 股"
            + (f" @ {_fmt_price(entry.avg_cost)}" if entry.avg_cost is not None else "")
            + (f"，浮盈 {_fmt_pnl(entry.unrealized_pnl)}" if entry.unrealized_pnl is not None else "")
        )
        rows.append(("持仓", pos))

    table = "\n".join(["| 指标 | 值 |", "|---|---|", *[f"| {k} | {v} |" for k, v in rows]])

    parts: list[str] = [header, "", "**触发信号**", sig_lines, "", "**关键数据**", table]

    # LLM analysis block — only if the strategy populated metadata.
    if sug is not None:
        provenance: list[str] = []
        if sug.client_name:
            provenance.append(sug.client_name)
        if sug.latency_ms is not None:
            provenance.append(f"{sug.latency_ms / 1000:.1f}s")
        if sug.fallback_used:
            provenance.append("⚠️ fallback")
        prov_str = " · ".join(provenance) if provenance else "—"

        parts.append("")
        parts.append(f"**LLM 分析** ({prov_str})")
        if sug.raw_llm_text:
            # Render the raw model output as a quoted block so it's clearly the model's voice.
            quoted = "\n".join(
                f"> {ln}" if ln.strip() else ">"
                for ln in sug.raw_llm_text.strip().splitlines()
            )
            parts.append(quoted)
        else:
            parts.append(f"> {sug.reason}")

        decision_line = (
            f"**决策**：{action_emoji(sug.action)} {sug.action}"
            + (f" {sug.qty}" if sug.qty else "")
            + (
                f" · confidence {sug.confidence:.2f}"
                if sug.confidence is not None
                else ""
            )
            + (" · ⚠️ 走了回退路径" if sug.fallback_used else "")
        )
        parts.append("")
        parts.append(decision_line)
    else:
        parts.append("")
        parts.append("**决策**：⚪ 无决策（策略未触发）")

    if entry.chart_image_path:
        parts.append("")
        parts.append(f"**K 线快照**：![chart]({entry.chart_image_path})")

    if entry.audit_log_path:
        parts.append("")
        parts.append(f"_审计参考：`{entry.audit_log_path}`_")

    return "\n".join(parts) + "\n"
