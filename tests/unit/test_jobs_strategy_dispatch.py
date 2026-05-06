"""Unit tests for jobs._run_strategy_per_code error isolation (C1).

A misbehaving strategy on one symbol must not abort the rest of the cron
tick — this is the same property `_execute_suggestions` already enforces
for broker-side errors, applied one layer earlier.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from vibe_trader.scheduler.jobs import _run_strategy_per_code
from vibe_trader.signals.base import Severity, Signal
from vibe_trader.signals.strategy_base import StrategyContext
from vibe_trader.signals.strategy_lite import SignalSuggest


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


# ---------------------------------------------------------------------------
# C2b: ctx population — verify the extra fields are wired through.
# ---------------------------------------------------------------------------


class _CtxRecorder:
    """Captures the StrategyContext passed to decide(); always returns None."""

    name = "recorder"

    def __init__(self) -> None:
        self.captured: list[StrategyContext] = []

    def decide(self, ctx: StrategyContext) -> SignalSuggest | None:
        self.captured.append(ctx)
        return None


def test_run_strategy_per_code_propagates_full_ctx() -> None:
    """C2b: kline_dfs / position_details / return_summaries must reach ctx."""
    import pandas as pd

    from vibe_trader.reports.interpret import ReturnSummary

    df = pd.DataFrame({"close": [100.0, 101.0]})
    rec = _CtxRecorder()
    _run_strategy_per_code(
        rec,
        {"US.AAPL": [_sig("US.AAPL")]},
        positions={"US.AAPL": 100},
        kline_dfs={"US.AAPL": df},
        position_details={"US.AAPL": (100, 175.50, 1234.0)},
        return_summaries={
            "US.AAPL": ReturnSummary(intraday=0.012, last_30_bars=-0.025)
        },
    )
    assert len(rec.captured) == 1
    ctx = rec.captured[0]
    assert ctx.code == "US.AAPL"
    assert ctx.position_qty == 100
    assert ctx.avg_cost == 175.50
    assert ctx.realized_pnl == 1234.0
    assert ctx.intraday_return == 0.012
    assert ctx.last_30_bar_return == -0.025
    assert ctx.kline_60m is df


def test_run_strategy_per_code_falls_back_to_positions_when_no_details() -> None:
    """`position_details` is optional; positions dict alone still yields qty."""
    rec = _CtxRecorder()
    _run_strategy_per_code(
        rec,
        {"US.AAPL": [_sig("US.AAPL")]},
        positions={"US.AAPL": 50},
        # no position_details/kline_dfs/return_summaries
    )
    ctx = rec.captured[0]
    assert ctx.position_qty == 50
    assert ctx.avg_cost == 0.0
    assert ctx.realized_pnl == 0.0
    assert ctx.intraday_return is None
    assert ctx.last_30_bar_return is None
    assert ctx.kline_60m is None
