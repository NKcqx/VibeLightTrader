"""Strategy abstraction layer.

This module defines the contract that any auto-trading strategy must
satisfy in order to plug into `run_intraday_check`. The current Phase B
implementation only ships a `RuleStrategy` (rule-based, hard-coded) but
the contract is deliberately wide enough to accommodate future
additions without breaking jobs.py:

  - LLMStrategy   — every cron tick prompts an LLM with the same context
                    and parses a JSON action; see strategy_llm.py
  - EnsembleStrategy — combines multiple strategies via voting / weights

Design notes
------------
- StrategyContext is the *only* parameter passed to `Strategy.decide`.
  When new data sources land (e.g. order-book imbalance, options flow)
  they get added here; downstream strategies can opt in by reading the
  new fields.
- Today's RuleStrategy only consumes `signals` and `position_qty`, which
  is a strict subset of the context — a future-proof choice that costs
  nothing now.
- Registry-based factory keeps `scheduler/jobs.py` decoupled from
  concrete implementations: it just asks for whichever strategy
  `cfg.trader.strategy.type` named.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import pandas as pd

from vibe_trader.data.fundamentals import Fundamentals
from vibe_trader.futu_client import Snapshot
from vibe_trader.signals.base import Signal
from vibe_trader.signals.strategy_lite import SignalSuggest


@dataclass(frozen=True)
class StrategyContext:
    """Per-symbol decision context handed to `Strategy.decide`.

    Mandatory fields are populated for every cron tick. Optional fields
    are best-effort; a Strategy should tolerate `None` (e.g. RuleStrategy
    only needs `signals` + `position_qty` and ignores the rest).
    """

    code: str
    signals: list[Signal]
    position_qty: int = 0

    # ----- optional enrichment (LLMStrategy / future strategies) -----
    snapshot: Snapshot | None = None
    """Live quote at decision time."""

    kline_60m: pd.DataFrame | None = None
    """Recent 60m OHLCV+indicators (last ~200 bars). Index is tz-naive UTC ts."""

    avg_cost: float = 0.0
    """Position avg_cost from `positions` table; 0 when no position."""

    realized_pnl: float = 0.0
    """Cumulative realized P&L for this symbol (cents-precise)."""

    intraday_return: float | None = None
    """(last_price - open_price) / open_price; None when open is unknown."""

    last_30_bar_return: float | None = None
    """30-bar trailing return on the 60m series; None when not enough history."""

    fundamentals: Fundamentals | None = None
    """Wall-Street consensus + recent rating changes + news + earnings calendar.
    Fed by `FundamentalsClient.fetch(code)` at scheduler tick time. Strategies
    that don't care (e.g. RuleStrategy) can ignore it."""

    config: dict[str, Any] = field(default_factory=dict)
    """Strategy-private knobs from settings.yaml `trader.strategy.<type>`."""


@runtime_checkable
class Strategy(Protocol):
    """Contract: produce zero or one trade suggestion per (symbol, tick).

    Returning `None` means "no opinion". Returning `SignalSuggest(action='HOLD', qty=0, ...)`
    means "I considered and chose to hold" — these are different in the
    audit log (None = silent, HOLD = explicit).
    """

    name: str
    """Stable identifier persisted to DB (`signals.strategy_name`,
    `trades.strategy_name` once those columns land in C3). Lowercase,
    no spaces. e.g. "rule", "llm", "ensemble:rule+llm"."""

    def decide(self, ctx: StrategyContext) -> SignalSuggest | None:
        """Examine the context and return a suggestion (or None).

        Implementations MUST be deterministic given identical input
        (LLM-based strategies achieve this by setting temperature=0 and
        a fixed seed where supported, plus seeded fallback paths).

        Implementations MUST NOT raise on bad data — return None and log
        a warning instead. The scheduler isolates strategy errors but
        relies on each strategy doing its own input validation.
        """
        ...


# ---------------------------------------------------------------------------
# Registry: lazy factory keyed by `cfg.trader.strategy.type`.
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, Any] = {}


def register_strategy(name: str):
    """Decorator: register a callable that builds a Strategy from a dict config.

    Usage:
        @register_strategy("rule")
        def _build_rule(cfg: dict) -> Strategy:
            return RuleStrategy(**cfg)
    """

    def deco(builder):
        if name in _REGISTRY:
            raise ValueError(f"strategy {name!r} already registered")
        _REGISTRY[name] = builder
        return builder

    return deco


def build_strategy(name: str, config: dict[str, Any] | None = None) -> Strategy:
    """Look up a registered builder and instantiate it.

    Raises KeyError with the available names if `name` is unknown.
    """
    if name not in _REGISTRY:
        available = ", ".join(sorted(_REGISTRY)) or "<none>"
        raise KeyError(
            f"unknown strategy {name!r}; registered: {available}"
        )
    return _REGISTRY[name](config or {})


def registered_strategies() -> list[str]:
    """All currently-registered strategy names (introspection / docs)."""
    return sorted(_REGISTRY)
