"""Unit tests for llm/prompt.py — render + JSON-tolerant parser.

Parser cases dominate; render tests are smoke-only because the template
is debugged interactively against real LLM responses.
"""

from __future__ import annotations

import pytest

from vibe_trader.llm.client import LLMParseError
from vibe_trader.llm.prompt import (
    DEFAULT_USER_TEMPLATE,
    ParsedDecision,
    parse_decision,
    render_user_prompt,
)


# ---------------------------------------------------------------------------
# parse_decision — happy path
# ---------------------------------------------------------------------------


def test_parses_clean_json() -> None:
    raw = '{"action":"BUY","qty":50,"confidence":0.82,"reason":"RSI 超卖+金叉"}'
    out = parse_decision(raw)
    assert out == ParsedDecision(
        action="BUY", qty=50, confidence=0.82, reason="RSI 超卖+金叉"
    )


def test_strips_fenced_code_block() -> None:
    raw = '```json\n{"action": "SELL", "qty": 30, "confidence": 0.7, "reason":"止盈"}\n```'
    assert parse_decision(raw).action == "SELL"


def test_extracts_from_prose_wrapped_response() -> None:
    raw = (
        "Sure, here's my analysis:\n"
        '{"action": "HOLD", "qty": 0, "confidence": 0.4, "reason": "震荡观望"}\n'
        "Let me know if you'd like more detail."
    )
    out = parse_decision(raw)
    assert out.action == "HOLD"
    assert out.qty == 0


def test_lowercase_action_normalized_to_uppercase() -> None:
    raw = '{"action": "buy", "qty": 10, "confidence": 0.9, "reason": "ok"}'
    assert parse_decision(raw).action == "BUY"


def test_int_confidence_promoted_to_float() -> None:
    raw = '{"action": "HOLD", "qty": 0, "confidence": 1, "reason": "ok"}'
    assert parse_decision(raw).confidence == 1.0


# ---------------------------------------------------------------------------
# parse_decision — failures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, match",
    [
        ("", "empty response text"),
        ("    ", "empty response text"),
        ("no json here at all, just prose", "no JSON object found"),
        ("{this is not valid json", "JSON decode error"),
        ('[1, 2, 3]', "no JSON object found"),  # array, not object — bare regex matches nothing balanced
        ('{"action": "SHORT", "qty": 10, "confidence": 0.8, "reason": "x"}', "invalid action"),
        ('{"action": "BUY", "qty": -5, "confidence": 0.8, "reason": "x"}', "invalid qty"),
        ('{"action": "BUY", "qty": 10, "confidence": 1.5, "reason": "x"}', "out of"),
        ('{"action": "BUY", "qty": 10, "confidence": -0.1, "reason": "x"}', "out of"),
        ('{"action": "HOLD", "qty": 5, "confidence": 0.8, "reason": "x"}', "HOLD must have qty=0"),
        ('{"action": "BUY", "qty": 10, "confidence": 0.8, "reason": ""}', "invalid reason"),
        ('{"action": "BUY", "confidence": 0.8, "reason": "x"}', "missing fields"),
    ],
)
def test_parse_decision_rejects_bad_input(raw: str, match: str) -> None:
    with pytest.raises(LLMParseError, match=match):
        parse_decision(raw)


# ---------------------------------------------------------------------------
# render_user_prompt — smoke
# ---------------------------------------------------------------------------


def test_render_prompt_contains_expected_fields() -> None:
    out = render_user_prompt(
        code="US.AAPL",
        snapshot=None,
        position_qty=100,
        avg_cost=175.50,
        realized_pnl=12.34,
        intraday_return=0.012,
        last_30_bar_return=-0.025,
        indicators={
            "rsi_14": 28.5,
            "macd": -0.12,
            "macd_signal": -0.05,
            "macd_hist": -0.07,
            "boll_upper": 291.0,
            "boll_mid": 283.0,
            "boll_lower": 275.0,
        },
        signals=[
            {"signal_type": "rsi_oversold", "severity": "WARN", "payload_summary": "rsi=28.50"},
            {"signal_type": "macd_death_cross", "severity": "WARN", "payload_summary": None},
        ],
        max_position=200,
        min_trade_size=10,
        min_confidence=0.6,
    )
    assert "US.AAPL" in out
    assert "qty: 100" in out
    assert "avg_cost: $175.50" in out
    assert "RSI(14):       28.50" in out
    assert "rsi_oversold (WARN)" in out
    assert "max_position:   200" in out


def test_render_prompt_handles_missing_indicators() -> None:
    out = render_user_prompt(
        code="US.AAPL",
        snapshot=None,
        position_qty=0,
        avg_cost=0.0,
        realized_pnl=0.0,
        intraday_return=None,
        last_30_bar_return=None,
        indicators=None,
        signals=[],
        max_position=200,
        min_trade_size=10,
        min_confidence=0.6,
    )
    assert "(none)" in out  # signals empty → "(none)"
    assert "Indicators" not in out  # indicators block skipped entirely
