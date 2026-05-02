from __future__ import annotations

import pytest

from equity_monitor.reports.interpret import (
    IndicatorReading,
    PositionSummary,
    ReturnSummary,
    build_diagnostics_md,
    reading_from_row,
)


def test_rsi_overbought_label() -> None:
    r = IndicatorReading(
        rsi_14=78.5, macd=None, macd_signal=None, macd_hist=None,
        boll_upper=None, boll_mid=None, boll_lower=None, close=100.0,
    )
    out = r.lines()
    assert any("超买" in line for line in out)
    assert any("78.5" in line for line in out)


def test_rsi_oversold_label() -> None:
    r = IndicatorReading(
        rsi_14=22.1, macd=None, macd_signal=None, macd_hist=None,
        boll_upper=None, boll_mid=None, boll_lower=None, close=100.0,
    )
    assert any("超卖" in line for line in r.lines())


def test_rsi_neutral_label() -> None:
    r = IndicatorReading(
        rsi_14=50.0, macd=None, macd_signal=None, macd_hist=None,
        boll_upper=None, boll_mid=None, boll_lower=None, close=100.0,
    )
    assert any("中性" in line for line in r.lines())


def test_macd_above_signal_means_golden() -> None:
    r = IndicatorReading(
        rsi_14=None, macd=0.5, macd_signal=0.2, macd_hist=0.3,
        boll_upper=None, boll_mid=None, boll_lower=None, close=100.0,
    )
    assert any("金叉" in line for line in r.lines())


def test_macd_below_signal_means_death() -> None:
    r = IndicatorReading(
        rsi_14=None, macd=-0.3, macd_signal=0.1, macd_hist=-0.4,
        boll_upper=None, boll_mid=None, boll_lower=None, close=100.0,
    )
    assert any("死叉" in line for line in r.lines())


def test_boll_above_upper() -> None:
    r = IndicatorReading(
        rsi_14=None, macd=None, macd_signal=None, macd_hist=None,
        boll_upper=180.0, boll_mid=170.0, boll_lower=160.0, close=190.0,
    )
    assert any("突破上轨" in line for line in r.lines())


def test_boll_below_lower() -> None:
    r = IndicatorReading(
        rsi_14=None, macd=None, macd_signal=None, macd_hist=None,
        boll_upper=180.0, boll_mid=170.0, boll_lower=160.0, close=155.0,
    )
    assert any("跌破下轨" in line for line in r.lines())


def test_boll_in_band_shows_position() -> None:
    r = IndicatorReading(
        rsi_14=None, macd=None, macd_signal=None, macd_hist=None,
        boll_upper=180.0, boll_mid=170.0, boll_lower=160.0, close=170.0,
    )
    out = r.lines()
    assert any("通道内" in line for line in out)
    assert any("50%" in line for line in out)


def test_indicator_skips_none_fields() -> None:
    """If RSI is None, no RSI line is emitted (don't crash, don't fake data)."""
    r = IndicatorReading(
        rsi_14=None, macd=0.5, macd_signal=0.2, macd_hist=0.3,
        boll_upper=None, boll_mid=None, boll_lower=None, close=100.0,
    )
    out = r.lines()
    assert not any("RSI" in line for line in out)
    assert any("MACD" in line for line in out)


def test_return_summary_intraday_only() -> None:
    rs = ReturnSummary(intraday=0.025, last_30_bars=None)
    line = rs.line()
    assert "▲" in line
    assert "+2.50%" in line
    assert "近 30" not in line


def test_return_summary_negative_arrow() -> None:
    rs = ReturnSummary(intraday=-0.014, last_30_bars=None)
    assert "▼" in rs.line()
    assert "-1.40%" in rs.line()


def test_return_summary_both() -> None:
    rs = ReturnSummary(intraday=0.025, last_30_bars=0.128)
    line = rs.line()
    assert "日内" in line and "近 30" in line


def test_position_summary_pnl_and_return_pct() -> None:
    p = PositionSummary(qty=50, avg_cost=178.30, mark=185.00)
    assert p.market_value == pytest.approx(9250.0)
    assert p.unrealized_pnl == pytest.approx(335.0)
    assert p.return_pct == pytest.approx((185 - 178.30) / 178.30)
    line = p.line()
    assert "50" in line and "+$335" in line and "+3.76%" in line


def test_position_summary_loss() -> None:
    p = PositionSummary(qty=20, avg_cost=200.0, mark=180.0)
    assert p.unrealized_pnl == pytest.approx(-400.0)
    line = p.line()
    assert "$-400" in line or "-$400" in line  # depends on sign formatting


def test_build_diagnostics_md_combines_all() -> None:
    md = build_diagnostics_md(
        indicator=IndicatorReading(
            rsi_14=25.0, macd=0.5, macd_signal=0.2, macd_hist=0.3,
            boll_upper=180.0, boll_mid=170.0, boll_lower=160.0, close=158.0,
        ),
        returns=ReturnSummary(intraday=-0.025, last_30_bars=0.04),
        position=PositionSummary(qty=10, avg_cost=170.0, mark=158.0),
    )
    assert "📈 走势" in md
    assert "📊 技术面" in md
    assert "💼" in md
    assert "RSI" in md and "BOLL" in md and "MACD" in md
    assert "持仓" in md


def test_build_diagnostics_md_returns_empty_when_all_none() -> None:
    assert build_diagnostics_md(indicator=None, returns=None, position=None) == ""


def test_reading_from_row_handles_nan() -> None:
    r = reading_from_row({"rsi_14": float("nan"), "macd": 0.1}, close=100.0)
    assert r.rsi_14 is None
    assert r.macd == 0.1


def test_reading_from_row_handles_missing_keys() -> None:
    r = reading_from_row({"rsi_14": 50.0}, close=100.0)
    assert r.macd is None
    assert r.boll_upper is None
