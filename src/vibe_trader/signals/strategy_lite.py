"""Lightweight rule-based decision engine: Signal(s) → SignalSuggest.

Phase 2 MVP. Strictly **deterministic, hand-coded rules** — no ML, no
historical optimization. Phase 3 replaces this with a real strategy module.

Inputs:
  - A batch of Signals emitted within a single `intraday_check` invocation
    for ONE symbol (caller groups by code).
  - Current open quantity in that symbol (for SELL caps and BUY ceilings).
  - Caller-supplied config: `max_position_per_symbol` (default 200) and
    base trade size (default 100 for CRITICAL, 50 for WARN combo).

Outputs:
  - One `SignalSuggest` per *batch* (rules collapse into a single decision)
    or `None` if no rule fires.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from vibe_trader.signals.base import Severity, Signal


@dataclass(frozen=True)
class SignalSuggest:
    """A single trade suggestion derived from one or more Signals.

    `triggering_signal_types`: the signal_types that actually drove the
    decision — used in the Lark card "reason" line.

    The `*_meta` block below is filled by LLM-backed strategies and stays
    None for rule-based ones. We keep it on the suggestion object (rather
    than carrying a separate object) so downstream consumers (Lark cards,
    journal writer, audit hooks) can read or ignore it without touching
    the strategy contract. All fields are optional — RuleStrategy and any
    pre-LLM caller continues to compile unchanged.
    """

    action: Literal["BUY", "SELL", "HOLD"]
    qty: int
    reason: str
    triggering_signal_types: tuple[str, ...]

    # Optional LLM/strategy metadata — populated by LLMStrategy, None elsewhere.
    confidence: float | None = None
    """0.0..1.0 if the strategy reports one (e.g. LLM). None for rule-based."""

    raw_llm_text: str | None = None
    """Verbatim assistant reply (pre-parse). For audit / journal display.
    Truncated to ~2KB by callers if needed; we don't truncate here."""

    latency_ms: int | None = None
    """End-to-end strategy.decide() wall time. None when not measured."""

    client_name: str | None = None
    """Stable strategy / client identifier for the journal:
    'cursor-agent:default', 'anthropic:claude-3-5-sonnet-20241022',
    'rule', 'rule(fallback)', 'llm-fallback:hold'."""

    fallback_used: bool = False
    """True iff the suggestion came from the strategy's fallback path
    (e.g. LLM timed out → RuleStrategy ran). Lets the journal mark
    'degraded' decisions distinctly."""


def decide_action(
    signals: list[Signal],
    *,
    current_qty: int = 0,
    max_position_per_symbol: int = 200,
    critical_size: int = 100,
    warn_size: int = 50,
    rsi_extreme: float = 30.0,
) -> SignalSuggest | None:
    """Apply the rule matrix to a batch of signals for one symbol.

    Returns the highest-priority suggestion, or None if no rule applies.

    Rule priority (top wins):
      1. threshold_breach_lower (CRITICAL) → BUY critical_size
      2. threshold_breach_upper (CRITICAL) → SELL all current_qty
      3. rsi_oversold + macd_golden_cross  → BUY warn_size
      4. rsi_overbought + macd_death_cross → SELL min(warn_size, current_qty)
      5. boll_lower_break + rsi < rsi_extreme (in payload) → HOLD (signal-only)
    """
    if not signals:
        return None

    by_type = {s.signal_type: s for s in signals}

    # Rule 1: lower threshold breach → BUY (most critical, support level)
    if "threshold_breach_lower" in by_type:
        capacity = max_position_per_symbol - current_qty
        qty = min(critical_size, max(0, capacity))
        if qty == 0:
            return SignalSuggest(
                action="HOLD",
                qty=0,
                reason=(
                    f"已满仓 ({current_qty}/{max_position_per_symbol})，"
                    "支撑位破位但跳过加仓"
                ),
                triggering_signal_types=("threshold_breach_lower",),
            )
        return SignalSuggest(
            action="BUY",
            qty=qty,
            reason="价格跌破用户支撑位，逢低吸纳",
            triggering_signal_types=("threshold_breach_lower",),
        )

    # Rule 2: upper threshold breach → SELL all
    if "threshold_breach_upper" in by_type:
        if current_qty == 0:
            return None  # nothing to sell, no suggestion
        return SignalSuggest(
            action="SELL",
            qty=current_qty,
            reason="价格突破用户阻力位，全部止盈",
            triggering_signal_types=("threshold_breach_upper",),
        )

    # Rule 3: RSI oversold + MACD golden cross → BUY (warn)
    if "rsi_oversold" in by_type and "macd_golden_cross" in by_type:
        capacity = max_position_per_symbol - current_qty
        qty = min(warn_size, max(0, capacity))
        if qty == 0:
            return None
        return SignalSuggest(
            action="BUY",
            qty=qty,
            reason="RSI 超卖 + MACD 金叉，技术面反转信号",
            triggering_signal_types=("rsi_oversold", "macd_golden_cross"),
        )

    # Rule 4: RSI overbought + MACD death cross → SELL (warn)
    if "rsi_overbought" in by_type and "macd_death_cross" in by_type:
        if current_qty == 0:
            return None
        qty = min(warn_size, current_qty)
        return SignalSuggest(
            action="SELL",
            qty=qty,
            reason="RSI 超买 + MACD 死叉，技术面反转信号",
            triggering_signal_types=("rsi_overbought", "macd_death_cross"),
        )

    # Rule 5: Bollinger lower-break + extreme RSI → HOLD (info-only)
    if "boll_lower_break" in by_type:
        rsi_sig = by_type.get("rsi_oversold")
        if rsi_sig and float(rsi_sig.payload.get("rsi", 100.0)) < rsi_extreme:
            return SignalSuggest(
                action="HOLD",
                qty=0,
                reason="布林下轨突破 + RSI 极端低位，观望反弹",
                triggering_signal_types=("boll_lower_break", "rsi_oversold"),
            )

    return None


def decide_actions_for_codes(
    signals_by_code: dict[str, list[Signal]],
    *,
    positions: dict[str, int] | None = None,
    max_position_per_symbol: int = 200,
    critical_size: int = 100,
    warn_size: int = 50,
) -> dict[str, SignalSuggest]:
    """Convenience wrapper: decide for many codes at once.

    `positions[code]` defaults to 0 if absent. Returns codes with non-None
    suggestions only.
    """
    positions = positions or {}
    out: dict[str, SignalSuggest] = {}
    for code, sigs in signals_by_code.items():
        decision = decide_action(
            sigs,
            current_qty=positions.get(code, 0),
            max_position_per_symbol=max_position_per_symbol,
            critical_size=critical_size,
            warn_size=warn_size,
        )
        if decision is not None:
            out[code] = decision
    return out


# severity hint for downstream display
SEVERITY_BY_ACTION = {
    "BUY": Severity.CRITICAL,
    "SELL": Severity.CRITICAL,
    "HOLD": Severity.INFO,
}
