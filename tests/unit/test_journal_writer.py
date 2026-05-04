"""Unit tests for the per-symbol journal writer.

Each test runs in its own tmp_path so we don't pollute the repo's
data/journal/ during CI. The writer is the only consumer of the
template module, so we exercise both layers together — easier to
catch breaking template tweaks here than to mock the renderer.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from equity_monitor.journal import (
    JournalEntry,
    OverviewSnapshot,
    append_event,
    refresh_overview_only,
)
from equity_monitor.journal.templates import (
    EVENT_DELIMITER,
    OVERVIEW_BEGIN,
    OVERVIEW_END,
    render_event,
    render_overview,
)
from equity_monitor.journal.writer import (
    compute_overview,
    scan_existing_events,
)
from equity_monitor.signals.base import Severity, Signal
from equity_monitor.signals.strategy_lite import SignalSuggest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_signal(
    code: str = "US.NVDA",
    signal_type: str = "rsi_overbought",
    severity: Severity = Severity.WARN,
    payload: dict | None = None,
) -> Signal:
    return Signal(
        code=code,
        signal_type=signal_type,
        severity=severity,
        ts=datetime(2026, 5, 4, 18, 30, tzinfo=timezone.utc),
        payload=payload or {"rsi": 71.3, "close": 198.45},
    )


def _make_suggestion(
    action: str = "HOLD",
    qty: int = 0,
    *,
    confidence: float | None = 0.72,
    fallback_used: bool = False,
    client_name: str | None = "cursor-agent:default",
    raw_text: str | None = "上轨破位关键信号且RSI近超买,追涨风险高,观望等待更优价位。",
) -> SignalSuggest:
    return SignalSuggest(
        action=action,
        qty=qty,
        reason="追涨风险高,观望等待更优价位",
        triggering_signal_types=("rsi_overbought",),
        confidence=confidence,
        raw_llm_text=raw_text,
        latency_ms=27300,
        client_name=client_name,
        fallback_used=fallback_used,
    )


def _make_entry(
    code: str = "US.NVDA",
    *,
    ts: datetime | None = None,
    suggestion: SignalSuggest | None = None,
    chart_image_path: str | None = None,
) -> JournalEntry:
    return JournalEntry(
        code=code,
        ts=ts or datetime(2026, 5, 4, 14, 30),
        last_price=198.45,
        intraday_pct=0.003,
        last_30_bar_pct=0.058,
        rsi_14=71.3,
        macd=0.5,
        macd_signal=1.0,
        macd_hist=-0.5,
        boll_upper=202.1,
        boll_mid=195.4,
        boll_lower=188.7,
        position_qty=100,
        avg_cost=185.2,
        unrealized_pnl=1325.0,
        signals=[_make_signal()],
        suggestion=suggestion if suggestion is not None else _make_suggestion(),
        audit_log_path="data/llm_decisions.jsonl",
        chart_image_path=chart_image_path,
    )


# ---------------------------------------------------------------------------
# Templates: render_event / render_overview
# ---------------------------------------------------------------------------


def test_render_event_includes_signals_and_decision_block():
    md = render_event(_make_entry())
    assert "## 2026-05-04 14:30 — 🟡 HOLD" in md
    assert "**触发信号**" in md
    assert "rsi_overbought" in md
    assert "**关键数据**" in md
    assert "**LLM 分析**" in md
    assert "cursor-agent:default" in md
    assert "27.3s" in md
    assert "**决策**" in md
    assert "confidence 0.72" in md
    assert "_审计参考：`data/llm_decisions.jsonl`_" in md


def test_render_event_buy_action_shows_qty_in_title():
    sug = _make_suggestion(action="BUY", qty=50)
    md = render_event(_make_entry(suggestion=sug))
    assert "🟢 BUY 50" in md
    assert "**决策**：🟢 BUY 50" in md


def test_render_event_marks_fallback_visibly():
    sug = _make_suggestion(fallback_used=True, client_name="cursor-agent→rule")
    md = render_event(_make_entry(suggestion=sug))
    assert "⚠️ fallback" in md
    assert "走了回退路径" in md


def test_render_event_chart_image_link_present_only_when_path_set():
    md_no_chart = render_event(_make_entry(chart_image_path=None))
    assert "K 线快照" not in md_no_chart

    md_with_chart = render_event(_make_entry(chart_image_path="var/snapshots/foo.png"))
    assert "**K 线快照**：![chart](var/snapshots/foo.png)" in md_with_chart


def test_render_event_no_suggestion_renders_no_decision_marker():
    entry = _make_entry()
    entry_no_sug = JournalEntry(**{**entry.__dict__, "suggestion": None})
    md = render_event(entry_no_sug)
    assert "## 2026-05-04 14:30 — ⚪ 无决策" in md
    assert "**LLM 分析**" not in md
    assert "**决策**：⚪ 无决策" in md


def test_render_overview_brackets_balanced_when_meta_present():
    """Regression guard: a previous bug used CN「（」+ EN「)」mix."""
    ov = OverviewSnapshot(
        code="US.NVDA",
        display_name="NVIDIA",
        last_check_ts=datetime(2026, 5, 4, 14, 30),
        last_price=198.45,
        intraday_pct=0.003,
        upper_threshold=220.0,
        lower_threshold=170.0,
        position_qty=100,
        avg_cost=185.2,
        unrealized_pnl=1325.0,
        total_events=1,
        counts_by_action={"HOLD": 1},
        fallback_count=0,
        last_decision_action="HOLD",
        last_decision_ts=datetime(2026, 5, 4, 14, 30),
        last_decision_client="cursor-agent:default",
        last_decision_confidence=0.72,
    )
    md = render_overview(ov)
    # Either both CN brackets, or both EN brackets — never mixed.
    assert "（cursor-agent:default，conf 0.72）" in md
    assert ")" not in md.split("最近决策")[1].split("\n")[0] or "（" not in md.split("最近决策")[1].split("\n")[0]


def test_render_overview_handles_no_position_and_no_decisions():
    ov = OverviewSnapshot(
        code="US.MSFT",
        display_name="Microsoft",
        last_check_ts=datetime(2026, 5, 4, 14, 30),
        last_price=414.20,
        intraday_pct=0.016,
        upper_threshold=480.0,
        lower_threshold=360.0,
        position_qty=0,
        avg_cost=None,
        unrealized_pnl=None,
        total_events=0,
        counts_by_action={},
        fallback_count=0,
        last_decision_action=None,
        last_decision_ts=None,
        last_decision_client=None,
        last_decision_confidence=None,
    )
    md = render_overview(ov)
    assert "无持仓" in md
    assert "尚无决策事件" in md
    assert OVERVIEW_BEGIN in md and OVERVIEW_END in md


# ---------------------------------------------------------------------------
# Writer: append_event / refresh_overview_only
# ---------------------------------------------------------------------------


def test_append_event_writes_file_with_header_overview_and_event(tmp_path):
    entry = _make_entry()
    ov = compute_overview(
        code="US.NVDA",
        display_name="NVIDIA",
        last_check_ts=entry.ts,
        last_price=198.45,
        intraday_pct=0.003,
        upper_threshold=220.0,
        lower_threshold=170.0,
        position_qty=100,
        avg_cost=185.2,
        unrealized_pnl=1325.0,
        journal_dir=tmp_path,
        new_entry=entry,
    )
    path = append_event(journal_dir=tmp_path, overview=ov, entry=entry)
    text = path.read_text(encoding="utf-8")

    assert path == tmp_path / "US.NVDA.md"
    assert text.startswith("# US.NVDA · NVIDIA")
    assert OVERVIEW_BEGIN in text and OVERVIEW_END in text
    assert "## 2026-05-04 14:30 — 🟡 HOLD" in text


def test_append_event_prepends_newer_entry_above_older(tmp_path):
    older = _make_entry(ts=datetime(2026, 5, 4, 13, 30))
    newer = _make_entry(ts=datetime(2026, 5, 4, 14, 30),
                        suggestion=_make_suggestion(action="BUY", qty=50))

    for e in (older, newer):
        ov = compute_overview(
            code="US.NVDA", display_name="NVIDIA",
            last_check_ts=e.ts, last_price=198.45, intraday_pct=0.003,
            upper_threshold=220.0, lower_threshold=170.0,
            position_qty=100, avg_cost=185.2, unrealized_pnl=1325.0,
            journal_dir=tmp_path, new_entry=e,
        )
        append_event(journal_dir=tmp_path, overview=ov, entry=e)

    text = (tmp_path / "US.NVDA.md").read_text(encoding="utf-8")
    # The newer (BUY 50) entry must appear before the older (HOLD) one.
    pos_buy = text.find("🟢 BUY 50")
    pos_hold = text.find("🟡 HOLD")
    assert 0 <= pos_buy < pos_hold


def test_refresh_overview_only_does_not_lose_existing_events(tmp_path):
    entry = _make_entry()
    ov_full = compute_overview(
        code="US.NVDA", display_name="NVIDIA",
        last_check_ts=entry.ts, last_price=198.45, intraday_pct=0.003,
        upper_threshold=220.0, lower_threshold=170.0,
        position_qty=100, avg_cost=185.2, unrealized_pnl=1325.0,
        journal_dir=tmp_path, new_entry=entry,
    )
    append_event(journal_dir=tmp_path, overview=ov_full, entry=entry)

    # Now refresh-only with a new check timestamp.
    later_ts = datetime(2026, 5, 4, 15, 30)
    ov_refresh = compute_overview(
        code="US.NVDA", display_name="NVIDIA",
        last_check_ts=later_ts, last_price=199.50, intraday_pct=0.005,
        upper_threshold=220.0, lower_threshold=170.0,
        position_qty=100, avg_cost=185.2,
        unrealized_pnl=(199.50 - 185.2) * 100,
        journal_dir=tmp_path, new_entry=None,
    )
    refresh_overview_only(journal_dir=tmp_path, overview=ov_refresh)

    text = (tmp_path / "US.NVDA.md").read_text(encoding="utf-8")
    # Old event still present.
    assert "## 2026-05-04 14:30 — 🟡 HOLD" in text
    # Overview reflects new price.
    assert "$199.50" in text
    # Total events stayed at 1 (the refresh wasn't supposed to add a count).
    overview_block = text.split(OVERVIEW_BEGIN, 1)[1].split(OVERVIEW_END, 1)[0]
    assert "1 次" in overview_block


def test_refresh_overview_only_creates_file_when_missing(tmp_path):
    ov = compute_overview(
        code="US.MSFT", display_name="Microsoft",
        last_check_ts=datetime(2026, 5, 4, 14, 30),
        last_price=414.20, intraday_pct=0.016,
        upper_threshold=480.0, lower_threshold=360.0,
        position_qty=0, avg_cost=None, unrealized_pnl=None,
        journal_dir=tmp_path, new_entry=None,
    )
    refresh_overview_only(journal_dir=tmp_path, overview=ov)

    text = (tmp_path / "US.MSFT.md").read_text(encoding="utf-8")
    assert text.startswith("# US.MSFT · Microsoft")
    assert "无持仓" in text
    assert "尚无决策事件" in text


def test_atomic_write_no_tmp_left_behind(tmp_path):
    entry = _make_entry()
    ov = compute_overview(
        code="US.NVDA", display_name="NVIDIA",
        last_check_ts=entry.ts, last_price=198.45, intraday_pct=0.003,
        upper_threshold=220.0, lower_threshold=170.0,
        position_qty=100, avg_cost=185.2, unrealized_pnl=1325.0,
        journal_dir=tmp_path, new_entry=entry,
    )
    append_event(journal_dir=tmp_path, overview=ov, entry=entry)

    # Sibling tmp files have prefix `.US.NVDA.md.<random>.tmp` — none should remain.
    leftover = list(tmp_path.glob(".US.NVDA.md.*.tmp"))
    assert leftover == []


# ---------------------------------------------------------------------------
# History scanning: action counts roll forward across writes.
# ---------------------------------------------------------------------------


def test_compute_overview_counts_existing_events(tmp_path):
    # Seed two prior events in the file.
    e1 = _make_entry(ts=datetime(2026, 5, 4, 13, 30),
                     suggestion=_make_suggestion(action="BUY", qty=50))
    e2 = _make_entry(ts=datetime(2026, 5, 4, 14, 30),
                     suggestion=_make_suggestion(action="HOLD"))
    for e in (e1, e2):
        ov = compute_overview(
            code="US.NVDA", display_name="NVIDIA",
            last_check_ts=e.ts, last_price=198.45, intraday_pct=0.003,
            upper_threshold=220.0, lower_threshold=170.0,
            position_qty=100, avg_cost=185.2, unrealized_pnl=1325.0,
            journal_dir=tmp_path, new_entry=e,
        )
        append_event(journal_dir=tmp_path, overview=ov, entry=e)

    # Now compute overview with a third (SELL).
    e3 = _make_entry(ts=datetime(2026, 5, 4, 15, 30),
                     suggestion=_make_suggestion(action="SELL", qty=20))
    ov3 = compute_overview(
        code="US.NVDA", display_name="NVIDIA",
        last_check_ts=e3.ts, last_price=198.45, intraday_pct=0.003,
        upper_threshold=220.0, lower_threshold=170.0,
        position_qty=80, avg_cost=185.2, unrealized_pnl=1064.0,
        journal_dir=tmp_path, new_entry=e3,
    )
    assert ov3.total_events == 3
    assert ov3.counts_by_action == {"BUY": 1, "HOLD": 1, "SELL": 1}
    assert ov3.fallback_count == 0
    assert ov3.last_decision_action == "SELL"


def test_compute_overview_counts_fallback_events(tmp_path):
    e_ok = _make_entry(suggestion=_make_suggestion(action="BUY", qty=50))
    e_fb = _make_entry(
        ts=datetime(2026, 5, 4, 15, 30),
        suggestion=_make_suggestion(
            action="HOLD",
            fallback_used=True,
            client_name="cursor-agent:default→rule",
        ),
    )
    for e in (e_ok, e_fb):
        ov = compute_overview(
            code="US.NVDA", display_name="NVIDIA",
            last_check_ts=e.ts, last_price=198.45, intraday_pct=0.003,
            upper_threshold=220.0, lower_threshold=170.0,
            position_qty=100, avg_cost=185.2, unrealized_pnl=1325.0,
            journal_dir=tmp_path, new_entry=e,
        )
        append_event(journal_dir=tmp_path, overview=ov, entry=e)

    # New, neutral compute should report fallback_count==1 from history.
    ov_now = compute_overview(
        code="US.NVDA", display_name="NVIDIA",
        last_check_ts=datetime(2026, 5, 4, 16, 30),
        last_price=198.45, intraday_pct=0.003,
        upper_threshold=220.0, lower_threshold=170.0,
        position_qty=100, avg_cost=185.2, unrealized_pnl=1325.0,
        journal_dir=tmp_path, new_entry=None,
    )
    assert ov_now.fallback_count == 1
    assert ov_now.counts_by_action == {"BUY": 1, "HOLD": 1}


def test_scan_existing_events_returns_empty_for_missing_file(tmp_path):
    assert scan_existing_events(tmp_path / "ghost.md") == []


def test_scan_existing_events_handles_no_signal_entries(tmp_path):
    """`无决策` entries should appear in the parse with action=None."""
    md = (
        "# US.NVDA · NVIDIA\n\n"
        f"{OVERVIEW_BEGIN}\n## ovl\n{OVERVIEW_END}\n\n"
        f"{EVENT_DELIMITER}\n\n"
        "## 2026-05-04 14:30 — ⚪ 无决策\n\n"
        "**触发信号**\n- nothing\n"
    )
    p = tmp_path / "US.NVDA.md"
    p.write_text(md, encoding="utf-8")
    parsed = scan_existing_events(p)
    assert len(parsed) == 1
    assert parsed[0].action is None


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


def test_path_resolution_rejects_path_traversal(tmp_path):
    """Hostile codes with `/` get sanitised, but a literal '..' path is rejected."""
    from equity_monitor.journal.writer import _path_for

    safe = _path_for(tmp_path, "US/NVDA")  # slash → underscore
    assert safe.name == "US_NVDA.md"

    with pytest.raises(ValueError, match=r"\.\."):
        _path_for(tmp_path, f"..{__import__('os').sep}etc{__import__('os').sep}passwd")
