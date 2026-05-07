from __future__ import annotations

from datetime import datetime, timezone

from vibe_trader.reports.render import (
    render_daily_brief,
    render_signal_alert,
)
from vibe_trader.signals.base import Severity, Signal


def _texts(card: dict) -> str:
    """Concatenate every textual content in a Lark card for substring assertions."""
    chunks: list[str] = []
    for e in card.get("elements", []):
        if not isinstance(e, dict):
            continue
        text = e.get("text")
        if isinstance(text, dict):
            content = text.get("content")
            if isinstance(content, str):
                chunks.append(content)
        for sub in e.get("elements") or []:
            if isinstance(sub, dict):
                content = sub.get("content")
                if isinstance(content, str):
                    chunks.append(content)
    return " ".join(chunks)


def test_signal_alert_card_structure_warn() -> None:
    sig = Signal(
        code="US.NVDA",
        ts=datetime(2026, 5, 2, 18, 30, tzinfo=timezone.utc),
        signal_type="rsi_oversold",
        severity=Severity.WARN,
        payload={"rsi": 28.4},
    )
    card = render_signal_alert(
        code="US.NVDA",
        ts=datetime(2026, 5, 2, 18, 30, tzinfo=timezone.utc),
        close=135.42,
        change_pct=-0.023,
        signals=[sig],
    )
    assert card["header"]["template"] == "orange"
    assert "US.NVDA" in card["header"]["title"]["content"]
    assert card["header"]["title"]["content"].startswith("⚠️ US.NVDA")
    body = _texts(card)
    assert "RSI 超卖" in body
    # New format: "RSI 28.40 低于 30" (was: "rsi=28.4")
    assert "28.40" in body
    assert "低于 30" in body
    # Meaning line must accompany feature line
    assert "反弹" in body
    assert "$135.42" in body
    assert "-2.30%" in body


def test_signal_alert_max_severity_wins() -> None:
    """When multiple signals fire, header color/emoji follow the highest severity."""
    sigs = [
        Signal(
            code="US.AAPL",
            ts=datetime(2026, 5, 2, 14, tzinfo=timezone.utc),
            signal_type="boll_upper_break",
            severity=Severity.INFO,
            payload={},
        ),
        Signal(
            code="US.AAPL",
            ts=datetime(2026, 5, 2, 14, tzinfo=timezone.utc),
            signal_type="threshold_breach_upper",
            severity=Severity.CRITICAL,
            payload={"close": 205.0, "upper": 200.0},
        ),
    ]
    card = render_signal_alert(
        code="US.AAPL",
        ts=datetime(2026, 5, 2, 14, tzinfo=timezone.utc),
        close=205.0,
        change_pct=0.012,
        signals=sigs,
    )
    assert card["header"]["template"] == "red"
    assert "🔴" in card["header"]["title"]["content"]


def test_daily_brief_rows_render() -> None:
    card = render_daily_brief(
        kind="收盘盘点",
        date_str="2026-05-02 (Fri)",
        rows=[
            {
                "code": "US.NVDA",
                "close": 135.42,
                "change_pct": -0.023,
                "signal_count": 2,
            },
            {
                "code": "US.AAPL",
                "close": 182.30,
                "change_pct": 0.008,
                "signal_count": 0,
            },
        ],
        summary_lines=["资金异动 Top3: NVDA / AMD / META"],
    )
    body = _texts(card)
    assert card["header"]["template"] == "blue"
    assert "收盘盘点" in card["header"]["title"]["content"]
    assert "US.NVDA" in body
    assert "US.AAPL" in body
    assert "$135.42" in body
    assert "$182.30" in body
    assert "资金异动 Top3" in body


def test_daily_brief_empty_rows_renders_without_crash() -> None:
    card = render_daily_brief(
        kind="开盘后1h盘点",
        date_str="2026-05-02 (Fri)",
        rows=[],
        summary_lines=[],
    )
    assert card["header"]["template"] == "blue"
