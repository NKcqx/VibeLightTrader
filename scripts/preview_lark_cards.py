"""One-shot Lark card preview.

Renders every card variant (signal_alert × 4 severities/suggestion
shapes, daily_brief × 2, watchlist_card × 1) with
fabricated data and pushes to the Lark receiver in
`config/settings.yaml`. Use to eyeball the visual styling end-to-end
without waiting for real signals to fire.

Usage:
    python scripts/preview_lark_cards.py
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from vibe_trader.config import load_settings  # noqa: E402
from vibe_trader.reports.lark import send_card  # noqa: E402
from vibe_trader.reports.render import (  # noqa: E402
    WatchlistCardRow,
    interpret_indicators,
    render_daily_brief,
    render_signal_alert,
    render_watchlist_card,
)
from vibe_trader.signals.base import Severity, Signal  # noqa: E402


def _now() -> datetime:
    return datetime.now(timezone.utc)


def main() -> None:
    cfg = load_settings(ROOT / "config" / "settings.yaml")
    open_id = cfg.lark.receiver.open_id
    rtype = cfg.lark.receiver.type
    cli_path = cfg.lark.cli_path
    identity = cfg.lark.identity

    cards: list[tuple[str, dict]] = []

    # ─── 1. signal_alert × 4 (INFO / WARN / CRITICAL × 2) ──────────────
    cards.append(
        (
            "signal_alert · INFO (MACD 金叉)",
            render_signal_alert(
                code="US.NVDA",
                ts=_now(),
                close=205.10,
                change_pct=0.0162,
                signals=[
                    Signal(
                        code="US.NVDA",
                        ts=_now(),
                        signal_type="macd_golden_cross",
                        severity=Severity.INFO,
                        payload={"macd": 0.85, "signal": 0.42, "hist": 0.43},
                    )
                ],
                diagnostics_md=interpret_indicators(
                    close=205.10, rsi=58.2,
                    macd=0.85, macd_signal=0.42, macd_hist=0.43,
                    boll_upper=210.0, boll_mid=200.0, boll_lower=190.0,
                ),
            ),
        )
    )

    cards.append(
        (
            "signal_alert · WARN (RSI 超买)",
            render_signal_alert(
                code="US.MSFT",
                ts=_now(),
                close=438.20,
                change_pct=0.0234,
                signals=[
                    Signal(
                        code="US.MSFT",
                        ts=_now(),
                        signal_type="rsi_overbought",
                        severity=Severity.WARN,
                        payload={"rsi": 72.4},
                    )
                ],
                diagnostics_md=interpret_indicators(
                    close=438.20, rsi=72.4,
                    macd=1.10, macd_signal=0.95, macd_hist=0.15,
                    boll_upper=440.0, boll_mid=425.0, boll_lower=410.0,
                ),
            ),
        )
    )

    cards.append(
        (
            "signal_alert · CRITICAL + LLM BUY 建议",
            render_signal_alert(
                code="US.AAPL",
                ts=_now(),
                close=164.20,
                change_pct=-0.0285,
                signals=[
                    Signal(
                        code="US.AAPL",
                        ts=_now(),
                        signal_type="threshold_breach_lower",
                        severity=Severity.CRITICAL,
                        payload={"close": 164.20, "lower": 165.0},
                    ),
                    Signal(
                        code="US.AAPL",
                        ts=_now(),
                        signal_type="rsi_oversold",
                        severity=Severity.WARN,
                        payload={"rsi": 28.6},
                    ),
                ],
                signal_ids=[7421],
                suggestion={
                    "action": "BUY",
                    "qty": 120,
                    "reason": "跌破下阈值且 RSI 超卖，LLM 判断为中长线建仓时机",
                    "signal_id": 7421,
                },
                diagnostics_md=interpret_indicators(
                    close=164.20, rsi=28.6,
                    macd=-0.55, macd_signal=-0.20, macd_hist=-0.35,
                    boll_upper=180.0, boll_mid=172.0, boll_lower=164.0,
                ),
            ),
        )
    )

    cards.append(
        (
            "signal_alert · CRITICAL + HOLD (LLM 谨慎)",
            render_signal_alert(
                code="US.TSLA",
                ts=_now(),
                close=251.30,
                change_pct=0.0412,
                signals=[
                    Signal(
                        code="US.TSLA",
                        ts=_now(),
                        signal_type="threshold_breach_upper",
                        severity=Severity.CRITICAL,
                        payload={"close": 251.30, "upper": 250.0},
                    ),
                ],
                signal_ids=[7422],
                suggestion={
                    "action": "HOLD",
                    "qty": 0,
                    "reason": "已穿上阈值但成交量未放大，等待回踩 245 再决定",
                    "signal_id": 7422,
                },
                diagnostics_md=interpret_indicators(
                    close=251.30, rsi=68.2,
                    macd=2.10, macd_signal=1.85, macd_hist=0.25,
                    boll_upper=252.0, boll_mid=240.0, boll_lower=228.0,
                ),
            ),
        )
    )

    # ─── 2. daily_brief × 2 (morning / closing) ───────────────────────
    rows = [
        {
            "code": "US.NVDA",
            "close": 205.10,
            "change_pct": 0.0162,
            "signal_count": 1,
            "analysis": "RSI 58 (中性) · MACD 多头 · BOLL 中轨上方",
            "pnl_str": "持仓 100 @ $182.40 · 浮盈 +$2,270",
        },
        {
            "code": "US.MSFT",
            "close": 438.20,
            "change_pct": 0.0234,
            "signal_count": 1,
            "analysis": "RSI 72 (超买) · MACD 多头 · BOLL 中轨上方",
            "pnl_str": "无持仓",
        },
        {
            "code": "US.AAPL",
            "close": 164.20,
            "change_pct": -0.0285,
            "signal_count": 2,
            "analysis": "RSI 29 (超卖) · MACD 空头 · BOLL 跌破下轨",
            "pnl_str": "持仓 120 @ $172.10 · 浮亏 -$948",
        },
    ]

    cards.append(
        (
            "daily_brief · morning_brief",
            render_daily_brief(
                kind="morning",
                date_str=_now().strftime("%Y-%m-%d"),
                rows=rows,
                summary_lines=[
                    "本日 watchlist 3 标的，1 跌破阈值，1 临近上阈值",
                    "LLM 倾向：US.AAPL 建仓，US.MSFT 观望，US.TSLA 临近止盈",
                ],
            ),
        )
    )

    cards.append(
        (
            "daily_brief · closing_brief (含 P&L)",
            render_daily_brief(
                kind="closing",
                date_str=_now().strftime("%Y-%m-%d"),
                rows=rows,
                summary_lines=[
                    "全天 watchlist 共触发 4 信号，3 笔 BUY 1 笔 HOLD",
                    "执行 1 笔 SIMULATE 单：BUY 120 US.AAPL @ $164.20",
                ],
                pnl_lines=[
                    "US.NVDA: +$2,270.00 (+12.4%)",
                    "US.AAPL: -$948.00 (-4.6%)",
                    "已实现 (近 7 天): +$1,540.00",
                ],
            ),
        )
    )

    # ─── 3. watchlist_card · /list 回复 ───────────────────────────────
    cards.append(
        (
            "watchlist_card · /list",
            render_watchlist_card(
                title="📋 当前监控列表",
                action_text="共 3 个标的",
                rows=[
                    WatchlistCardRow(
                        code="US.NVDA",
                        body_md=(
                            "**$205.10** ▲ +1.62%  阈值: $170 ~ $220\n"
                            "  📊 RSI 58 (中性) · MACD 多头 · BOLL 中轨上方"
                        ),
                    ),
                    WatchlistCardRow(
                        code="US.MSFT",
                        body_md=(
                            "**$438.20** ▲ +2.34%  阈值: $400 ~ $450\n"
                            "  📊 RSI 72 (超买) · MACD 多头 · BOLL 中轨上方"
                        ),
                    ),
                    WatchlistCardRow(
                        code="US.AAPL",
                        body_md=(
                            "**$164.20** ▼ -2.85%  阈值: $165 ~ $200 ⚠️ 跌破下阈值\n"
                            "  📊 RSI 29 (超卖) · MACD 空头 · BOLL 跌破下轨"
                        ),
                    ),
                ],
                ts=_now(),
                color="blue",
                footer_md="改阈值: `阈值 US.NVDA 上限220 下限170`",
            ),
        )
    )

    # ─── push them ────────────────────────────────────────────────────
    print(f"Pushing {len(cards)} preview cards to {rtype}={open_id} ...")
    for i, (label, card) in enumerate(cards, 1):
        try:
            msg_id = send_card(
                card,
                open_id=open_id,
                receiver_type=rtype,
                cli_path=cli_path,
                identity=identity,
            )
            print(f"  [{i}/{len(cards)}] ✓ {label}  → msg_id={msg_id}")
        except Exception as e:  # noqa: BLE001
            print(f"  [{i}/{len(cards)}] ✗ {label}  → {e}")
        time.sleep(1.5)

    print("done.")


if __name__ == "__main__":
    main()
