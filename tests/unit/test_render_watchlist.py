from __future__ import annotations

from datetime import datetime, timezone

from equity_monitor.reports.render import (
    WatchlistCardRow,
    render_watchlist_card,
)


def test_render_watchlist_card_minimum_shape() -> None:
    card = render_watchlist_card(
        title="监控列表",
        action_text="📭 监控列表为空。",
        rows=[],
        ts=datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc),
    )
    assert card["config"]["wide_screen_mode"] is True
    assert card["header"]["title"]["content"] == "📋 监控列表"
    assert card["header"]["template"] == "blue"
    elements = card["elements"]
    # Always: action_text div + ts note
    assert any("监控列表为空" in str(el) for el in elements)


def test_render_watchlist_card_with_rows() -> None:
    rows = [
        WatchlistCardRow(
            code="US.AAPL",
            body_md=(
                "**`US.AAPL`** Apple\n"
                "💰 **$280.14** ▲ +0.46%\n"
                "🎯 上限 200 / 下限 165\n"
                "📊 RSI 53.8 中性 · MACD 金叉 · BOLL 通道内 (69%)"
            ),
        ),
        WatchlistCardRow(code="US.NVDA", body_md="**`US.NVDA`** NVIDIA\n💰 $198.45"),
    ]
    card = render_watchlist_card(
        title="监控列表 (2)",
        action_text="📋 当前监控 2 个标的:",
        rows=rows,
        ts=datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc),
    )
    body = str(card)
    assert "US.AAPL" in body
    assert "US.NVDA" in body
    assert "RSI 53.8" in body
    assert "$280.14" in body


def test_render_watchlist_card_with_footer() -> None:
    card = render_watchlist_card(
        title="已添加",
        action_text="✅ 已添加 US.AAPL",
        rows=[],
        ts=datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc),
        color="green",
        footer_md="提示: 5 分钟内 OpenD 抓不到数据将留空",
    )
    assert card["header"]["template"] == "green"
    body = str(card)
    assert "提示" in body


def test_render_watchlist_card_handles_special_chars_in_action() -> None:
    """Quotes / backticks / newlines must survive jinja escape into JSON."""
    card = render_watchlist_card(
        title="t",
        action_text='ℹ️ `US.AAPL` 已在监控中（无变化"。',
        rows=[],
        ts=datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc),
    )
    found = False
    for el in card["elements"]:
        if isinstance(el, dict) and el.get("tag") == "div":
            if "已在监控中" in el["text"]["content"]:
                found = True
                break
    assert found
