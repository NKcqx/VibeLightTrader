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
                "today_signals": [
                    {
                        "signal_type": "macd_golden_cross",
                        "severity": "WARN",
                        "ts": datetime(2026, 5, 2, 14, 0, tzinfo=timezone.utc),
                    },
                    {
                        "signal_type": "rsi_oversold",
                        "severity": "INFO",
                        "ts": datetime(2026, 5, 2, 18, 0, tzinfo=timezone.utc),
                    },
                ],
                "analysis": "RSI 32 (偏弱) · MACD 多头 · BOLL 中轨下方",
            },
            {
                "code": "US.AAPL",
                "close": 182.30,
                "change_pct": 0.008,
                "today_signals": [],
                "analysis": "RSI 55 (中性) · MACD 多头 · BOLL 中轨上方",
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
    # 新格式：用具体的"今日信号"替代了原本的"信号:N"那个废话
    assert "信号:0" not in body and "信号:2" not in body
    assert "🔔 今日信号:" in body
    # NVDA 触发了 MACD 金叉 (WARN) + RSI 超卖 (INFO)，按 severity 排序，WARN 在前
    assert "⚠️MACD金叉" in body
    assert "ℹ️RSI超卖" in body
    # AAPL 当天没信号 → 显示 "✨ 平静"
    assert "✨ 平静" in body
    # 📊 指标行使用 analysis 字段做兜底
    assert "📊 指标:" in body
    assert "RSI 32 (偏弱)" in body


def test_daily_brief_today_signals_truncates_with_overflow_marker() -> None:
    """超过 max_show 的信号要被折叠成 (+N)。"""
    sigs = [
        {"signal_type": "rsi_overbought", "severity": "INFO"},
        {"signal_type": "rsi_oversold", "severity": "INFO"},
        {"signal_type": "macd_golden_cross", "severity": "INFO"},
        {"signal_type": "macd_death_cross", "severity": "INFO"},
        {"signal_type": "boll_upper_break", "severity": "INFO"},
    ]
    card = render_daily_brief(
        kind="开盘后1h盘点",
        date_str="2026-05-02 (Fri)",
        rows=[
            {
                "code": "US.NVDA",
                "close": 135.42,
                "change_pct": 0.012,
                "today_signals": sigs,
            }
        ],
    )
    body = _texts(card)
    # 5 条只列前 3 条 + (+2)
    assert "(+2)" in body


def test_daily_brief_uses_indicator_reading_when_present() -> None:
    """传入 IndicatorReading 时 📊 行应来自 interpret_indicators，而非 analysis。"""
    from vibe_trader.reports.interpret import IndicatorReading

    ind = IndicatorReading(
        rsi_14=72.0,
        macd=1.5,
        macd_signal=1.2,
        macd_hist=0.3,
        boll_upper=210.0,
        boll_mid=200.0,
        boll_lower=190.0,
        close=215.0,  # > upper
    )
    card = render_daily_brief(
        kind="开盘后1h盘点",
        date_str="2026-05-02 (Fri)",
        rows=[
            {
                "code": "US.NVDA",
                "close": 215.0,
                "change_pct": 0.05,
                "indicator": ind,
                "analysis": "should-not-render-because-indicator-wins",
                "today_signals": [],
            }
        ],
    )
    body = _texts(card)
    assert "RSI 72 (超买)" in body
    assert "BOLL 突破上轨" in body
    assert "should-not-render-because-indicator-wins" not in body


def test_daily_brief_empty_rows_renders_without_crash() -> None:
    card = render_daily_brief(
        kind="开盘后1h盘点",
        date_str="2026-05-02 (Fri)",
        rows=[],
        summary_lines=[],
    )
    assert card["header"]["template"] == "blue"
