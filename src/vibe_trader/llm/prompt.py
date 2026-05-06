"""Prompt rendering + JSON-tolerant decision parsing.

The strategy layer passes a `StrategyContext` through `render_user_prompt`
to get the user-message string sent to the LLM, then runs the model's
reply through `parse_decision` to extract a `ParsedDecision`. Both steps
are deliberately kept out of `strategy_llm.py` so they can be unit-tested
in isolation against a hundred adversarial inputs.

Defaults are tuned for short, deterministic, JSON-only outputs at
temperature=0. Customising the prompt is a config-only change once we
expose `prompt_override` on StrategyLLMConfig (deferred — YAGNI today).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from jinja2 import StrictUndefined, Template

from vibe_trader.llm.client import LLMParseError


# ---------------------------------------------------------------------------
# Prompts.
# ---------------------------------------------------------------------------

DEFAULT_SYSTEM_PROMPT = """\
You are a disciplined US-equity paper-trading assistant. Your job is to
output ONE trading decision per request, encoded as a single JSON object.

Hard rules — violating any disqualifies your response:
  1. Output EXACTLY one JSON object, no Markdown fences, no prose, no
     explanation outside the "reason" field.
  2. Schema (all fields required):
     {
       "action":     "BUY" | "SELL" | "HOLD",
       "qty":        integer >= 0,
       "confidence": float in [0.0, 1.0],
       "reason":     string in Chinese, <= 80 characters
     }
  3. Choose HOLD when confidence < min_confidence given in the user message.
  4. Never recommend SELL with qty > current position size.
  5. Never recommend BUY that would push total position above max_position.
  6. qty MUST be 0 when action is HOLD.
  7. When an "Investor profile" block is present in the user message,
     weight your decision toward that horizon and risk tolerance — do
     NOT short-term-scalp a multi-month thesis.

You will be evaluated on (a) JSON validity, (b) constraint adherence, and
(c) decision quality vs hand-coded rules over many trading days.
"""


DEFAULT_USER_TEMPLATE = """\
Symbol: {{ code }}
{%- if profile %}

Investor profile (medium-term framing):
  - horizon:                 {{ profile.horizon_months_min }}-{{ profile.horizon_months_max }} months ({{ profile.style }})
  - thesis:                  {{ profile.theme }}
  - budget per symbol:       ${{ '%.0f' % profile.budget_per_symbol_usd }}
  - max drawdown tolerated:  {{ profile.drawdown_tolerance_pct }}%
  - max single-symbol concentration: {{ profile.max_concentration_pct }}% of deployed capital
  - entry policy:            initial buy = {{ profile.initial_entry_pct }}% of budget; up to {{ profile.max_batches }} accumulating buys; add-on requires ≥{{ profile.add_on_dip_pct }}% dip and ≥{{ profile.add_cooldown_days }}d cooldown; prefer_dip_buy={{ profile.prefer_dip_buy }}
  - exit policy:             take-profit at +{{ profile.take_profit_pct }}% (trim {{ profile.take_profit_trim_pct }}%); hard-stop at -{{ profile.hard_stop_pct }}%; min_holding_days={{ profile.min_holding_days }}
{%- endif %}

Live quote:
{%- if snapshot %}
  - last_price: ${{ '%.2f' % snapshot.last_price }}
  {%- if intraday_return is not none %}
  - intraday_return: {{ '%+.2f' % (intraday_return * 100) }}%
  {%- endif %}
  {%- if last_30_bar_return is not none %}
  - 30-bar return:  {{ '%+.2f' % (last_30_bar_return * 100) }}%
  {%- endif %}
{%- else %}
  (no live snapshot available)
{%- endif %}

Position:
  - qty: {{ position_qty }}
  - avg_cost: ${{ '%.2f' % avg_cost }}
  - realized_pnl: ${{ '%.2f' % realized_pnl }}

{%- if indicators %}

Indicators (latest 60m bar):
  - RSI(14):       {{ '%.2f' % indicators.rsi_14 if indicators.rsi_14 is not none else 'n/a' }}
  - MACD:          {{ '%.4f' % indicators.macd if indicators.macd is not none else 'n/a' }} / signal={{ '%.4f' % indicators.macd_signal if indicators.macd_signal is not none else 'n/a' }} / hist={{ '%.4f' % indicators.macd_hist if indicators.macd_hist is not none else 'n/a' }}
  - Bollinger(20): lower={{ '%.2f' % indicators.boll_lower if indicators.boll_lower is not none else 'n/a' }} mid={{ '%.2f' % indicators.boll_mid if indicators.boll_mid is not none else 'n/a' }} upper={{ '%.2f' % indicators.boll_upper if indicators.boll_upper is not none else 'n/a' }}
{%- endif %}

Triggered signals (last {{ dedupe_window_minutes }} min):
{%- if signals %}
{%- for s in signals %}
  - {{ s.signal_type }} ({{ s.severity }}){% if s.payload_summary %} — {{ s.payload_summary }}{% endif %}
{%- endfor %}
{%- else %}
  (none)
{%- endif %}

Constraints:
  - max_position:   {{ max_position }}
  - min_trade_size: {{ min_trade_size }}
  - min_confidence: {{ min_confidence }}

Output the decision JSON now.
"""


def render_user_prompt(
    *,
    code: str,
    snapshot: Any | None,
    position_qty: int,
    avg_cost: float,
    realized_pnl: float,
    intraday_return: float | None,
    last_30_bar_return: float | None,
    indicators: dict[str, float | None] | None,
    signals: list[dict[str, Any]],
    max_position: int,
    min_trade_size: int,
    min_confidence: float,
    dedupe_window_minutes: int = 60,
    profile: Any | None = None,
    template: str = DEFAULT_USER_TEMPLATE,
) -> str:
    """Render the user message for one decision.

    `signals` is a list of dicts (not `Signal` objects) so callers can
    pre-summarise the payload in a Chinese-friendly form.
    `indicators` keys: rsi_14 / macd / macd_signal / macd_hist /
    boll_upper / boll_mid / boll_lower (any may be None).
    `profile`: an `InvestmentProfileConfig` (or any object exposing the
    same field names). When non-None and `profile.enabled`, the LLM
    receives the medium-term thesis framing block. Pass None to keep the
    legacy short-term framing.
    """
    if profile is not None and not getattr(profile, "enabled", True):
        profile = None
    tpl = Template(template, undefined=StrictUndefined)
    return tpl.render(
        code=code,
        snapshot=snapshot,
        position_qty=position_qty,
        avg_cost=avg_cost,
        realized_pnl=realized_pnl,
        intraday_return=intraday_return,
        last_30_bar_return=last_30_bar_return,
        indicators=indicators,
        signals=signals,
        max_position=max_position,
        min_trade_size=min_trade_size,
        min_confidence=min_confidence,
        dedupe_window_minutes=dedupe_window_minutes,
        profile=profile,
    )


# ---------------------------------------------------------------------------
# Parsing.
# ---------------------------------------------------------------------------

# Match the first balanced-looking JSON object. Greedy match within a
# fenced code block first; otherwise grab any { ... } chunk.
_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_BARE_RE = re.compile(r"\{[\s\S]*\}", re.DOTALL)

_VALID_ACTIONS = {"BUY", "SELL", "HOLD"}


@dataclass(frozen=True)
class ParsedDecision:
    """Validated decision shape — what the strategy layer consumes."""

    action: Literal["BUY", "SELL", "HOLD"]
    qty: int
    confidence: float
    reason: str


def parse_decision(text: str) -> ParsedDecision:
    """Pull a Decision JSON out of an LLM response and validate it.

    Raises:
        LLMParseError: when no JSON found, JSON malformed, fields
            missing/wrong type, action not in {BUY,SELL,HOLD}, qty
            negative, confidence out of [0,1], or HOLD with qty>0.
    """
    if not text or not text.strip():
        raise LLMParseError("empty response text")

    raw = text.strip()
    candidate: str | None = None

    fence = _FENCE_RE.search(raw)
    if fence:
        candidate = fence.group(1)
    elif raw.startswith("{"):
        candidate = raw
    else:
        m = _BARE_RE.search(raw)
        if m:
            candidate = m.group(0)

    if candidate is None:
        raise LLMParseError(f"no JSON object found in response: {raw[:200]!r}")

    try:
        obj = json.loads(candidate)
    except json.JSONDecodeError as e:
        raise LLMParseError(
            f"JSON decode error: {e}; payload={candidate[:200]!r}"
        ) from e

    if not isinstance(obj, dict):
        raise LLMParseError(f"expected JSON object, got {type(obj).__name__}")

    missing = {"action", "qty", "confidence", "reason"} - obj.keys()
    if missing:
        raise LLMParseError(f"missing fields: {sorted(missing)}; got {list(obj)}")

    action = obj["action"]
    if not isinstance(action, str) or action.upper() not in _VALID_ACTIONS:
        raise LLMParseError(f"invalid action {action!r}")
    action = action.upper()

    qty = obj["qty"]
    if not isinstance(qty, int) or qty < 0:
        raise LLMParseError(f"invalid qty {qty!r}")

    if action == "HOLD" and qty != 0:
        raise LLMParseError(f"HOLD must have qty=0, got {qty}")

    conf = obj["confidence"]
    if isinstance(conf, int):
        conf = float(conf)
    if not isinstance(conf, float) or not (0.0 <= conf <= 1.0):
        raise LLMParseError(f"confidence out of [0,1]: {conf!r}")

    reason = obj["reason"]
    if not isinstance(reason, str) or not reason.strip():
        raise LLMParseError(f"invalid reason {reason!r}")

    return ParsedDecision(
        action=action,  # type: ignore[arg-type]
        qty=qty,
        confidence=conf,
        reason=reason.strip(),
    )
