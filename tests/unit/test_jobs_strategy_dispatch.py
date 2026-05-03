"""Unit tests for jobs._run_strategy_per_code error isolation (C1).

A misbehaving strategy on one symbol must not abort the rest of the cron
tick — this is the same property `_execute_suggestions` already enforces
for broker-side errors, applied one layer earlier.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from equity_monitor.scheduler.jobs import _run_strategy_per_code
from equity_monitor.signals.base import Severity, Signal
from equity_monitor.signals.strategy_base import StrategyContext
from equity_monitor.signals.strategy_lite import SignalSuggest


class _FlakyStrategy:
    """Crashes on AAPL, returns BUY for everything else."""

    name = "flaky"

    def decide(self, ctx: StrategyContext) -> SignalSuggest | None:
        if ctx.code == "US.AAPL":
            raise RuntimeError("simulated LLM 500")
        return SignalSuggest(
            action="BUY", qty=10, reason="ok", triggering_signal_types=("x",)
        )


def _sig(code: str) -> Signal:
    return Signal(
        code=code,
        ts=datetime(2026, 5, 4, 13, 30, tzinfo=timezone.utc),
        signal_type="x",
        severity=Severity.WARN,
        payload={},
    )


def test_strategy_crash_on_one_symbol_skips_only_that_symbol() -> None:
    sigs_by_code = {
        "US.AAPL": [_sig("US.AAPL")],
        "US.NVDA": [_sig("US.NVDA")],
        "US.TSLA": [_sig("US.TSLA")],
    }
    out = _run_strategy_per_code(
        _FlakyStrategy(), sigs_by_code, positions={}
    )

    assert "US.AAPL" not in out, "AAPL crashed → no suggestion (silently skipped)"
    assert out["US.NVDA"].action == "BUY"
    assert out["US.TSLA"].action == "BUY"


class _NeverDecide:
    name = "abstain"

    def decide(self, ctx: StrategyContext) -> SignalSuggest | None:
        return None


def test_none_decisions_are_excluded_from_output() -> None:
    out = _run_strategy_per_code(
        _NeverDecide(),
        {"US.AAPL": [_sig("US.AAPL")]},
        positions={"US.AAPL": 50},
    )
    assert out == {}
