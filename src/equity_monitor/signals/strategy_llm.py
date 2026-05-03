"""LLM-driven trading strategy.

Architecture (one decision per `(code, tick)`):

    StrategyContext
        └─► render_user_prompt ─► [system, user] messages
                                     │
                                     ▼
                                LLMClient.chat
                                     │
                                     ▼
                              parse_decision
                                     │
                                     ▼
                          enforce_constraints
                                     │
                            ┌────────┴────────┐
                            ▼                 ▼
                  audit_log.write       SignalSuggest

Failure modes — each falls through to the configured `fallback_on_error`
(default `rule`):
    LLMTimeoutError, LLMHTTPError, LLMAuthError, LLMRateLimitError,
    LLMParseError, ConstraintViolation, any unexpected Exception.

Caching:
    Identical (code, ts-bucket-of-cache_seconds, hash-of-recent-signals)
    → reuse last decision. Saves 70%+ of LLM calls when re-rendering /
    re-running the same tick (common during dev). Cache is process-local;
    a fresh `equity-monitor run` starts cold.

Audit log:
    Every decision (LLM-driven OR fallback) appends one JSONL line to
    `data/llm_decisions.jsonl`. Append-only, NDJSON, safe to grep.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from equity_monitor.llm import (
    DEFAULT_SYSTEM_PROMPT,
    DEFAULT_USER_TEMPLATE,
    LLMClient,
    LLMError,
    Message,
    ParsedDecision,
    build_llm_client,
    parse_decision,
    render_user_prompt,
)
from equity_monitor.signals.strategy_base import (
    Strategy,
    StrategyContext,
    register_strategy,
)
from equity_monitor.signals.strategy_lite import SignalSuggest
from equity_monitor.signals.strategy_rule import RuleStrategy  # for fallback default

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Constraint guard — second-line defence against bad LLM output.
# Even if parse_decision passed, we re-validate against current
# position / config-level limits before letting it through.
# ---------------------------------------------------------------------------


class ConstraintViolation(Exception):
    """LLM proposed a trade that violates max_position / qty / confidence rules."""


def enforce_constraints(
    parsed: ParsedDecision,
    *,
    position_qty: int,
    max_position: int,
    min_trade_size: int,
    min_confidence: float,
) -> SignalSuggest:
    """Validate `parsed` against runtime constraints and emit SignalSuggest.

    HOLD always passes (qty=0, confidence either way). BUY/SELL must:
      - confidence >= min_confidence (else demoted to HOLD with same reason)
      - qty >= min_trade_size
      - BUY: position_qty + qty <= max_position
      - SELL: qty <= position_qty

    Demotions (low confidence) are NOT failures; they return HOLD.
    Real violations (qty out of bounds) raise ConstraintViolation, which
    triggers the fallback path.
    """
    if parsed.action == "HOLD":
        return SignalSuggest(
            action="HOLD",
            qty=0,
            reason=f"[llm] {parsed.reason}",
            triggering_signal_types=("llm_decision",),
        )

    if parsed.confidence < min_confidence:
        return SignalSuggest(
            action="HOLD",
            qty=0,
            reason=(
                f"[llm] 置信度 {parsed.confidence:.2f} < {min_confidence:.2f}，"
                f"原建议: {parsed.action} {parsed.qty} ({parsed.reason})"
            ),
            triggering_signal_types=("llm_low_confidence",),
        )

    if parsed.qty < min_trade_size:
        raise ConstraintViolation(
            f"qty {parsed.qty} below min_trade_size {min_trade_size}"
        )

    if parsed.action == "BUY":
        if position_qty + parsed.qty > max_position:
            raise ConstraintViolation(
                f"BUY {parsed.qty} would push position {position_qty}→"
                f"{position_qty + parsed.qty} above max_position {max_position}"
            )
    else:  # SELL
        if parsed.qty > position_qty:
            raise ConstraintViolation(
                f"SELL {parsed.qty} > current position {position_qty}"
            )

    return SignalSuggest(
        action=parsed.action,
        qty=parsed.qty,
        reason=f"[llm] {parsed.reason} (置信度 {parsed.confidence:.2f})",
        triggering_signal_types=("llm_decision",),
    )


# ---------------------------------------------------------------------------
# Audit log — append-only NDJSON.
# ---------------------------------------------------------------------------


def _append_audit(path: Path, record: dict[str, Any]) -> None:
    """Append one decision record. Best-effort; never raises."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:  # pragma: no cover — io failures are non-fatal
        log.warning("llm_audit.write_failed", path=str(path), error=repr(e))


# ---------------------------------------------------------------------------
# Strategy.
# ---------------------------------------------------------------------------


def _signal_payload_summary(payload: dict[str, Any]) -> str | None:
    """Tiny one-line summary of a signal payload for the prompt."""
    if not payload:
        return None
    parts: list[str] = []
    for k in ("rsi", "close", "macd_hist", "boll_upper", "boll_lower"):
        if k in payload:
            v = payload[k]
            if isinstance(v, float):
                parts.append(f"{k}={v:.2f}")
            else:
                parts.append(f"{k}={v}")
    return ", ".join(parts) or None


def _cache_key(ctx: StrategyContext, bucket_s: int) -> str:
    """Stable hash of (code, time-bucket, signals-fingerprint, position)."""
    if bucket_s <= 0:
        return ""  # caching disabled
    bucket = int(time.time() // bucket_s)
    sig_fp = ",".join(
        f"{s.signal_type}:{s.severity.value}" for s in ctx.signals
    )
    raw = f"{ctx.code}|{bucket}|{ctx.position_qty}|{sig_fp}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass
class LLMStrategy:
    """LLM-driven Strategy with hard-rule fallback.

    Construction parameters mirror the corresponding fields in
    `StrategyLLMConfig`. Most callers should use `build_strategy("llm",
    cfg.llm.model_dump())` rather than instantiating directly.
    """

    client: LLMClient
    fallback: Strategy

    name: str = "llm"

    max_position: int = 200
    min_trade_size: int = 10
    min_confidence: float = 0.6

    max_tokens: int = 512
    temperature: float = 0.0
    timeout_s: float = 30.0

    cache_seconds: int = 300

    audit_log_path: Path = field(default_factory=lambda: Path("data/llm_decisions.jsonl"))
    fallback_on_error: str = "rule"  # "rule" | "hold"

    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    user_template: str = DEFAULT_USER_TEMPLATE

    _cache: dict[str, SignalSuggest] = field(default_factory=dict, init=False, repr=False)

    def decide(self, ctx: StrategyContext) -> SignalSuggest | None:
        """One decision; never raises (errors → fallback path + audit).

        Empty `ctx.signals` returns None (we only ask the LLM when there's
        something to react to — preserves the 'no opinion' semantic and
        saves tokens).
        """
        if not ctx.signals:
            return None

        ck = _cache_key(ctx, self.cache_seconds)
        if ck and ck in self._cache:
            return self._cache[ck]

        prompt = self._build_messages(ctx)
        record: dict[str, Any] = {
            "ts_unix": time.time(),
            "code": ctx.code,
            "client": getattr(self.client, "name", "?"),
            "model": getattr(self.client, "model", "?"),
            "position_qty": ctx.position_qty,
            "signals": [s.signal_type for s in ctx.signals],
        }

        try:
            response = self.client.chat(
                prompt,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                timeout_s=self.timeout_s,
            )
            record["prompt_tokens"] = response.prompt_tokens
            record["completion_tokens"] = response.completion_tokens
            record["raw_text"] = response.text

            parsed = parse_decision(response.text)
            record["parsed"] = {
                "action": parsed.action,
                "qty": parsed.qty,
                "confidence": parsed.confidence,
                "reason": parsed.reason,
            }

            decision = enforce_constraints(
                parsed,
                position_qty=ctx.position_qty,
                max_position=self.max_position,
                min_trade_size=self.min_trade_size,
                min_confidence=self.min_confidence,
            )
            record["decision"] = {
                "action": decision.action,
                "qty": decision.qty,
                "reason": decision.reason,
            }
            record["fallback_used"] = False

        except (LLMError, ConstraintViolation, Exception) as e:
            record["error"] = {
                "type": type(e).__name__,
                "message": str(e)[:500],
            }
            decision = self._fallback_decision(ctx)
            record["fallback_used"] = True
            record["fallback_path"] = self.fallback_on_error
            record["decision"] = (
                {
                    "action": decision.action,
                    "qty": decision.qty,
                    "reason": decision.reason,
                }
                if decision is not None
                else None
            )
            log.warning(
                "llm_strategy.fallback",
                code=ctx.code,
                exc_type=type(e).__name__,
                fallback=self.fallback_on_error,
                error=str(e)[:200],
            )

        _append_audit(self.audit_log_path, record)
        if ck and decision is not None:
            self._cache[ck] = decision
        return decision

    # -------------------- helpers --------------------

    def _build_messages(self, ctx: StrategyContext) -> list[Message]:
        signals_view = [
            {
                "signal_type": s.signal_type,
                "severity": s.severity.value,
                "payload_summary": _signal_payload_summary(s.payload),
            }
            for s in ctx.signals
        ]
        indicators: dict[str, float | None] | None = None
        if ctx.kline_60m is not None and not ctx.kline_60m.empty:
            try:
                last = ctx.kline_60m.iloc[-1]
                indicators = {
                    "rsi_14": _opt_float(last.get("rsi_14")),
                    "macd": _opt_float(last.get("macd")),
                    "macd_signal": _opt_float(last.get("macd_signal")),
                    "macd_hist": _opt_float(last.get("macd_hist")),
                    "boll_upper": _opt_float(last.get("boll_upper")),
                    "boll_mid": _opt_float(last.get("boll_mid")),
                    "boll_lower": _opt_float(last.get("boll_lower")),
                }
            except Exception:  # pragma: no cover — be paranoid; tolerate odd df shapes
                indicators = None

        user = render_user_prompt(
            code=ctx.code,
            snapshot=ctx.snapshot,
            position_qty=ctx.position_qty,
            avg_cost=ctx.avg_cost,
            realized_pnl=ctx.realized_pnl,
            intraday_return=ctx.intraday_return,
            last_30_bar_return=ctx.last_30_bar_return,
            indicators=indicators,
            signals=signals_view,
            max_position=self.max_position,
            min_trade_size=self.min_trade_size,
            min_confidence=self.min_confidence,
            template=self.user_template,
        )
        return [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user},
        ]

    def _fallback_decision(self, ctx: StrategyContext) -> SignalSuggest | None:
        if self.fallback_on_error == "hold":
            return SignalSuggest(
                action="HOLD",
                qty=0,
                reason="[llm fallback=hold] LLM 失败，跳过本次决策",
                triggering_signal_types=("llm_fallback",),
            )
        # default: delegate to the rule strategy
        try:
            return self.fallback.decide(ctx)
        except Exception as e:  # pragma: no cover - rule strategy is robust
            log.error("llm_strategy.fallback_crash", error=repr(e))
            return None


def _opt_float(v: Any) -> float | None:
    """Return float(v) or None if v is NaN / None / not numeric."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    # NaN check without numpy: NaN != NaN
    if f != f:
        return None
    return f


# ---------------------------------------------------------------------------
# Registration. Importing this module makes "llm" available to
# `build_strategy()`. Idempotent.
# ---------------------------------------------------------------------------


def _build_llm_strategy(config: dict[str, Any]) -> Strategy:
    cfg = dict(config)  # don't mutate caller's dict
    provider = cfg.pop("provider", "anthropic")
    model = cfg.pop("model", "claude-3-5-sonnet-20241022")
    api_key_env = cfg.pop("api_key_env", "ANTHROPIC_API_KEY")
    base_url = cfg.pop("base_url", None)

    fallback_on_error = cfg.pop("fallback_on_error", "rule")
    # rule sub-knobs would belong here if we let users tune the fallback;
    # for now a default RuleStrategy() is fine — the rule path is only
    # hit on errors and uses the same SignalSuggest contract.
    fallback = RuleStrategy()

    client = build_llm_client(
        provider=provider,
        model=model,
        api_key_env=api_key_env,
        base_url=base_url,
    )

    audit_path = Path(cfg.pop("audit_log_path", "data/llm_decisions.jsonl"))

    return LLMStrategy(
        client=client,
        fallback=fallback,
        max_position=cfg.pop("max_position_per_symbol", 200),
        min_trade_size=cfg.pop("min_trade_size", 10),
        min_confidence=cfg.pop("min_confidence", 0.6),
        max_tokens=cfg.pop("max_tokens", 512),
        temperature=cfg.pop("temperature", 0.0),
        timeout_s=float(cfg.pop("timeout_s", 30)),
        cache_seconds=cfg.pop("cache_seconds", 300),
        audit_log_path=audit_path,
        fallback_on_error=fallback_on_error,
        # Skeleton fields from StrategyLLMConfig that we don't use today
        # are silently ignored — keeps yaml forward-compat. (kline_window,
        # news_window_minutes, news_top_k, max_concurrent, retries)
    )


try:
    register_strategy("llm")(_build_llm_strategy)
except ValueError:
    # already registered (test reload); fine.
    pass
