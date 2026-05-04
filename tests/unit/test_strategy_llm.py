"""Unit tests for LLMStrategy — happy / parse-fail / timeout / fallback /
constraint-violation / cache / audit-log paths.

Uses `FakeLLMClient` (no httpx mocking — clients are tested separately
in test_llm_clients.py) so each test is fast and deterministic.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from equity_monitor.llm.client import (
    LLMResponse,
    LLMTimeoutError,
)
from equity_monitor.signals.base import Severity, Signal
from equity_monitor.signals.strategy_base import (
    StrategyContext,
    build_strategy,
    registered_strategies,
)
from equity_monitor.signals.strategy_llm import (
    ConstraintViolation,
    LLMStrategy,
    enforce_constraints,
)
from equity_monitor.signals.strategy_rule import RuleStrategy


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


class FakeLLMClient:
    """In-memory stand-in for any LLMClient.

    `script` is consumed in order; each entry is either a string (returned
    as `LLMResponse.text`), an Exception (raised), or an LLMResponse
    object (returned verbatim). Once exhausted the test fails noisily.
    """

    def __init__(self, *, model: str = "fake-model", script: list[Any] | None = None) -> None:
        self.model = model
        self.name = f"fake:{model}"
        self.calls: list[dict[str, Any]] = []
        self._script = list(script or [])

    def chat(
        self, messages: list[dict[str, str]], *, max_tokens: int, temperature: float, timeout_s: float
    ) -> LLMResponse:
        self.calls.append(
            {
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "timeout_s": timeout_s,
            }
        )
        if not self._script:
            raise AssertionError("FakeLLMClient: script exhausted")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, LLMResponse):
            return item
        return LLMResponse(text=item, raw={"text": item})


def _ctx(*, code: str = "US.AAPL", qty: int = 0, signals: list[Signal] | None = None) -> StrategyContext:
    sigs = signals or [
        Signal(
            code=code,
            ts=datetime(2026, 5, 3, 10, 30, tzinfo=timezone.utc),
            signal_type="rsi_oversold",
            severity=Severity.WARN,
            payload={"rsi": 27.5, "close": 280.5},
        )
    ]
    return StrategyContext(code=code, signals=sigs, position_qty=qty)


def _strategy(
    *,
    client: FakeLLMClient,
    audit_path: Path,
    fallback_on_error: str = "rule",
    cache_seconds: int = 0,
    min_confidence: float = 0.6,
    max_position: int = 200,
    min_trade_size: int = 10,
) -> LLMStrategy:
    return LLMStrategy(
        client=client,
        fallback=RuleStrategy(),
        cache_seconds=cache_seconds,
        audit_log_path=audit_path,
        fallback_on_error=fallback_on_error,
        min_confidence=min_confidence,
        max_position=max_position,
        min_trade_size=min_trade_size,
    )


def _read_audit(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Path 1: happy path — LLM returns valid JSON, constraint pass.
# ---------------------------------------------------------------------------


def test_happy_path_returns_buy_decision_and_writes_audit(tmp_path: Path) -> None:
    audit = tmp_path / "decisions.jsonl"
    client = FakeLLMClient(
        script=['{"action":"BUY","qty":50,"confidence":0.85,"reason":"超卖反弹"}']
    )
    strat = _strategy(client=client, audit_path=audit)

    out = strat.decide(_ctx())

    assert out is not None
    assert out.action == "BUY"
    assert out.qty == 50
    assert "[llm]" in out.reason
    assert out.triggering_signal_types == ("llm_decision",)
    assert len(client.calls) == 1
    # Decision and signal context recorded
    rows = _read_audit(audit)
    assert len(rows) == 1
    assert rows[0]["fallback_used"] is False
    assert rows[0]["parsed"]["action"] == "BUY"
    assert rows[0]["decision"]["qty"] == 50


def test_no_signals_returns_none_without_calling_llm(tmp_path: Path) -> None:
    audit = tmp_path / "decisions.jsonl"
    client = FakeLLMClient(script=[])  # no calls allowed
    strat = _strategy(client=client, audit_path=audit)

    ctx = StrategyContext(code="US.AAPL", signals=[], position_qty=0)
    assert strat.decide(ctx) is None
    assert client.calls == []
    assert _read_audit(audit) == []


# ---------------------------------------------------------------------------
# Path 2: low confidence demoted to HOLD (not a fallback, intentional behavior).
# ---------------------------------------------------------------------------


def test_low_confidence_demoted_to_hold(tmp_path: Path) -> None:
    audit = tmp_path / "decisions.jsonl"
    client = FakeLLMClient(
        script=['{"action":"BUY","qty":50,"confidence":0.40,"reason":"略偏多"}']
    )
    strat = _strategy(client=client, audit_path=audit, min_confidence=0.6)

    out = strat.decide(_ctx())
    assert out is not None
    assert out.action == "HOLD"
    assert out.qty == 0
    assert "置信度" in out.reason
    rows = _read_audit(audit)
    assert rows[0]["fallback_used"] is False  # demotion is not a fallback
    assert rows[0]["decision"]["action"] == "HOLD"


# ---------------------------------------------------------------------------
# Path 3: parse error → fallback (rule).
# ---------------------------------------------------------------------------


def test_parse_error_falls_back_to_rule(tmp_path: Path) -> None:
    audit = tmp_path / "decisions.jsonl"
    client = FakeLLMClient(script=["this is just prose, no json"])
    strat = _strategy(client=client, audit_path=audit)

    # Use a signal that the rule strategy actually fires on (CRITICAL
    # threshold_breach_lower → BUY critical_size).
    sig = Signal(
        code="US.AAPL",
        ts=datetime(2026, 5, 3, tzinfo=timezone.utc),
        signal_type="threshold_breach_lower",
        severity=Severity.CRITICAL,
        payload={"close": 160.0, "lower": 165.0},
    )
    ctx = StrategyContext(code="US.AAPL", signals=[sig], position_qty=0)

    out = strat.decide(ctx)

    assert out is not None
    assert out.action == "BUY"  # rule fallback fired
    rows = _read_audit(audit)
    assert rows[0]["fallback_used"] is True
    assert rows[0]["error"]["type"] == "LLMParseError"
    assert rows[0]["fallback_path"] == "rule"


# ---------------------------------------------------------------------------
# Path 4: LLM timeout → fallback respects fallback_on_error.
# ---------------------------------------------------------------------------


def test_timeout_with_hold_fallback(tmp_path: Path) -> None:
    audit = tmp_path / "decisions.jsonl"
    client = FakeLLMClient(script=[LLMTimeoutError("slow", provider="fake")])
    strat = _strategy(client=client, audit_path=audit, fallback_on_error="hold")

    out = strat.decide(_ctx())
    assert out is not None
    assert out.action == "HOLD"
    assert "fallback=hold" in out.reason
    rows = _read_audit(audit)
    assert rows[0]["fallback_used"] is True
    assert rows[0]["error"]["type"] == "LLMTimeoutError"


def test_timeout_with_rule_fallback_uses_rule_path(tmp_path: Path) -> None:
    audit = tmp_path / "decisions.jsonl"
    # Build a context whose signals trigger the rule path: a CRITICAL
    # threshold_breach_lower → rule says BUY 100. We assert the LLM
    # strategy on timeout returns exactly what the rule would.
    sig = Signal(
        code="US.AAPL",
        ts=datetime(2026, 5, 3, tzinfo=timezone.utc),
        signal_type="threshold_breach_lower",
        severity=Severity.CRITICAL,
        payload={"close": 160.0, "lower": 165.0},
    )
    ctx = StrategyContext(code="US.AAPL", signals=[sig], position_qty=0)

    client = FakeLLMClient(script=[LLMTimeoutError("slow", provider="fake")])
    strat = _strategy(client=client, audit_path=audit, fallback_on_error="rule")

    out = strat.decide(ctx)
    assert out is not None
    # RuleStrategy(default critical_size=100) → BUY 100
    assert out.action == "BUY"
    assert out.qty == 100


# ---------------------------------------------------------------------------
# Path 5: constraint violation (LLM proposes too-big SELL) → fallback.
# ---------------------------------------------------------------------------


def test_constraint_violation_triggers_fallback(tmp_path: Path) -> None:
    audit = tmp_path / "decisions.jsonl"
    client = FakeLLMClient(
        script=['{"action":"SELL","qty":300,"confidence":0.9,"reason":"清仓"}']
    )
    strat = _strategy(
        client=client, audit_path=audit, fallback_on_error="hold"
    )

    # Position is only 50 → SELL 300 is impossible.
    ctx = _ctx(qty=50)
    out = strat.decide(ctx)
    assert out is not None
    assert out.action == "HOLD"  # fallback=hold
    rows = _read_audit(audit)
    assert rows[0]["error"]["type"] == "ConstraintViolation"


# ---------------------------------------------------------------------------
# Path 6: cache hit avoids second LLM call.
# ---------------------------------------------------------------------------


def test_cache_avoids_second_llm_call(tmp_path: Path) -> None:
    audit = tmp_path / "decisions.jsonl"
    client = FakeLLMClient(
        script=['{"action":"BUY","qty":50,"confidence":0.9,"reason":"x"}']
    )
    strat = _strategy(client=client, audit_path=audit, cache_seconds=600)

    out1 = strat.decide(_ctx())
    out2 = strat.decide(_ctx())

    assert out1 == out2
    assert len(client.calls) == 1, "second call should hit cache, not LLM"
    # Audit only logs the actual LLM-driven call:
    assert len(_read_audit(audit)) == 1


# ---------------------------------------------------------------------------
# enforce_constraints — direct unit tests.
# ---------------------------------------------------------------------------


def _parsed(action: str, qty: int, conf: float = 0.8, reason: str = "ok"):
    from equity_monitor.llm.prompt import ParsedDecision
    return ParsedDecision(action=action, qty=qty, confidence=conf, reason=reason)  # type: ignore[arg-type]


def test_enforce_buy_within_max_position_passes() -> None:
    out = enforce_constraints(
        _parsed("BUY", 50, conf=0.9),
        position_qty=100, max_position=200, min_trade_size=10, min_confidence=0.6,
    )
    assert out.action == "BUY" and out.qty == 50


def test_enforce_buy_exceeding_max_position_raises() -> None:
    with pytest.raises(ConstraintViolation, match="max_position"):
        enforce_constraints(
            _parsed("BUY", 150, conf=0.9),
            position_qty=100, max_position=200, min_trade_size=10, min_confidence=0.6,
        )


def test_enforce_sell_exceeding_position_raises() -> None:
    with pytest.raises(ConstraintViolation, match="SELL"):
        enforce_constraints(
            _parsed("SELL", 200, conf=0.9),
            position_qty=100, max_position=200, min_trade_size=10, min_confidence=0.6,
        )


def test_enforce_below_min_trade_size_raises() -> None:
    with pytest.raises(ConstraintViolation, match="min_trade_size"):
        enforce_constraints(
            _parsed("BUY", 5, conf=0.9),
            position_qty=0, max_position=200, min_trade_size=10, min_confidence=0.6,
        )


def test_enforce_hold_always_passes() -> None:
    out = enforce_constraints(
        _parsed("HOLD", 0, conf=0.0),
        position_qty=0, max_position=200, min_trade_size=10, min_confidence=0.6,
    )
    assert out.action == "HOLD" and out.qty == 0


# ---------------------------------------------------------------------------
# Registry sanity — "llm" is reachable through build_strategy.
# Build path uses real factory, so we have to set ANTHROPIC_API_KEY to
# something to avoid the AnthropicClient guard at construction time?
# Actually the guard is only at chat() time, not __init__, so this works
# even without an env var. We only assert the registry plumbing.
# ---------------------------------------------------------------------------


def test_llm_strategy_is_registered() -> None:
    assert "llm" in registered_strategies()


def test_build_strategy_llm_returns_LLMStrategy(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-not-used-no-network")
    s = build_strategy(
        "llm",
        {
            "provider": "anthropic",
            "model": "claude-3-5-haiku-20241022",
            "max_position_per_symbol": 200,
            "min_trade_size": 10,
            "min_confidence": 0.6,
        },
    )
    assert isinstance(s, LLMStrategy)
    assert s.name == "llm"
    assert s.max_position == 200


def test_build_strategy_llm_with_cursor_agent_provider(monkeypatch) -> None:
    """`provider: cursor-agent` builds an LLMStrategy backed by CursorAgentClient.

    No API key required. Workspace defaults to cwd. We assert plumbing
    only — actual subprocess invocation is covered in test_llm_cursor_agent.py.
    """
    from equity_monitor.llm.cursor_agent import CursorAgentClient

    s = build_strategy(
        "llm",
        {
            "provider": "cursor-agent",
            "model": "sonnet-4",
            "api_key_env": "",
            "cursor_agent_workspace": "/tmp/some_repo",
            "cursor_agent_extra_flags": ["--mode", "plan"],
            "max_position_per_symbol": 150,
            "min_trade_size": 10,
            "min_confidence": 0.7,
            "timeout_s": 240,
        },
    )
    assert isinstance(s, LLMStrategy)
    assert isinstance(s.client, CursorAgentClient)
    assert s.client.model == "sonnet-4"
    assert s.client.workspace == "/tmp/some_repo"
    assert s.client.extra_flags == ("--mode", "plan")
    assert s.max_position == 150
    assert s.min_confidence == 0.7
    assert s.timeout_s == 240


def test_build_strategy_llm_cursor_agent_workspace_defaults_to_cwd(tmp_path, monkeypatch) -> None:
    """When workspace is not given, falls back to Path.cwd() at build time."""
    from equity_monitor.llm.cursor_agent import CursorAgentClient

    monkeypatch.chdir(tmp_path)
    s = build_strategy(
        "llm",
        {
            "provider": "cursor-agent",
            "model": "",
            "api_key_env": "",
        },
    )
    assert isinstance(s.client, CursorAgentClient)
    assert s.client.workspace == str(tmp_path.resolve())
