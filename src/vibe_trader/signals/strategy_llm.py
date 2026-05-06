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
    a fresh `vibe-trader run` starts cold.

Audit log:
    Every decision (LLM-driven OR fallback) appends one JSONL line to
    `data/llm_decisions.jsonl`. Append-only, NDJSON, safe to grep.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import structlog

from vibe_trader.llm import (
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
from vibe_trader.signals.strategy_base import (
    Strategy,
    StrategyContext,
    register_strategy,
)
from vibe_trader.signals.strategy_lite import SignalSuggest
from vibe_trader.signals.strategy_rule import RuleStrategy  # for fallback default

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


def _decorate(
    decision: SignalSuggest | None,
    *,
    client_name: str,
    confidence: float | None = None,
    raw_text: str | None = None,
    latency_ms: int | None = None,
    fallback_used: bool = False,
) -> SignalSuggest | None:
    """Stamp LLM/strategy metadata onto a SignalSuggest before returning.

    Used both on the LLM happy path and on the fallback path so journal /
    audit / Lark consumers can reliably inspect *who decided this* and
    *was a fallback used*. SignalSuggest is frozen, so we replace().

    `confidence` is *not* overridden if the underlying decision already
    carries one (the constraint demotion path may set its own); only
    fill when the source said nothing. `raw_text` is truncated to 4 KB
    to keep the journal markdown readable — full text remains in the
    audit log if needed.
    """
    if decision is None:
        return None
    raw = raw_text
    if raw is not None and len(raw) > 4096:
        raw = raw[:4096] + "…(truncated)"
    new_confidence = (
        decision.confidence if decision.confidence is not None else confidence
    )
    return replace(
        decision,
        confidence=new_confidence,
        raw_llm_text=raw,
        latency_ms=latency_ms,
        client_name=client_name,
        fallback_used=fallback_used,
    )


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

    dev_log_path: Path = field(default_factory=lambda: Path("data/dev_log.md"))
    """Where to append a Markdown entry on every error / fallback. The
    file is created lazily and is for engineers, not end users — see
    `journal.errors` for the format."""

    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    user_template: str = DEFAULT_USER_TEMPLATE

    investment_profile: Any | None = None
    """Optional InvestmentProfileConfig (or any object exposing the same
    field names). When set + `enabled`, every prompt carries the
    medium-term thesis framing block. None = legacy short-term framing.
    """

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
        client_name = getattr(self.client, "name", "?")
        t0 = time.monotonic()
        record: dict[str, Any] = {
            "ts_unix": time.time(),
            "code": ctx.code,
            "client": client_name,
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
            decision = _decorate(
                decision,
                client_name=client_name,
                confidence=parsed.confidence,
                raw_text=response.text,
                latency_ms=int((time.monotonic() - t0) * 1000),
                fallback_used=False,
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
            decision = _decorate(
                decision,
                client_name=f"{client_name}→{self.fallback_on_error}",
                latency_ms=int((time.monotonic() - t0) * 1000),
                fallback_used=True,
            )
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
            self._write_dev_log(ctx, e, client_name)

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
            profile=self.investment_profile,
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

    def _write_dev_log(
        self,
        ctx: StrategyContext,
        exc: Exception,
        client_name: str,
    ) -> None:
        """Append a Markdown incident entry for engineers.

        Lazy import keeps the LLM strategy independent of the journal
        layer at module-load time (avoids a cycle if a future journal
        component imports this module).
        """
        try:
            from datetime import datetime, timezone

            from vibe_trader.journal.errors import (
                DevLogEntry,
                append_dev_log_entry,
                classify_exception,
            )

            raw_excerpt: str | None = None
            # If we have an LLMResponse-like object on the exception
            # chain, surface its raw text. Most errors don't carry that
            # so this is best-effort.
            cause_msg = (str(exc) or repr(exc))[:300]
            entry = DevLogEntry(
                ts=datetime.now(tz=timezone.utc),
                code=ctx.code,
                category=classify_exception(exc),
                client=client_name,
                error_type=type(exc).__name__,
                message=cause_msg,
                raw_excerpt=raw_excerpt,
            )
            append_dev_log_entry(dev_log_path=self.dev_log_path, entry=entry)
        except Exception as e:  # pragma: no cover — never let the dev-log writer kill a tick
            log.warning("llm_strategy.dev_log_failed", error=repr(e))


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

    # cursor-agent specific knobs (ignored by other providers).
    workspace = cfg.pop("cursor_agent_workspace", None)
    cursor_bin = cfg.pop("cursor_agent_binary", "cursor-agent")
    cursor_extra_flags = tuple(cfg.pop("cursor_agent_extra_flags", ()))
    # If the user picked cursor-agent without specifying a workspace,
    # default to the current working directory (which is the repo root
    # when running `vibe-trader` from inside the project — the only
    # supported invocation pattern, see README "How to run").
    if provider == "cursor-agent" and not workspace:
        workspace = str(Path.cwd().resolve())

    client = build_llm_client(
        provider=provider,
        model=model,
        api_key_env=api_key_env,
        base_url=base_url,
        workspace=workspace,
        cursor_agent_binary=cursor_bin,
        cursor_agent_extra_flags=cursor_extra_flags,
    )

    audit_path = Path(cfg.pop("audit_log_path", "data/llm_decisions.jsonl"))
    dev_log_path = Path(cfg.pop("dev_log_path", "data/dev_log.md"))

    # Pop the optional investor profile (passed via build_strategy by the
    # scheduler / CLI). Tests that drive _build_llm_strategy directly
    # without a profile keep their existing behaviour.
    profile = cfg.pop("investment_profile", None)

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
        dev_log_path=dev_log_path,
        fallback_on_error=fallback_on_error,
        investment_profile=profile,
        # Skeleton fields from StrategyLLMConfig that we don't use today
        # are silently ignored — keeps yaml forward-compat. (kline_window,
        # news_window_minutes, news_top_k, max_concurrent, retries)
    )


try:
    register_strategy("llm")(_build_llm_strategy)
except ValueError:
    # already registered (test reload); fine.
    pass
