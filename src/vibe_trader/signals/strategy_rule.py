"""Rule-based strategy: thin Strategy adapter over `strategy_lite.decide_action`.

Pure delegation. Behaviour is byte-identical to the pre-Phase-C1 hard-coded
call site in `scheduler/jobs.py`; this exists so that `cfg.trader.strategy.type
= "rule"` (the default) flows through the same Registry/factory plumbing as
any future LLM/ensemble strategy.

Side-effect on import: registers itself under the name "rule".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from vibe_trader.signals.strategy_base import (
    Strategy,
    StrategyContext,
    register_strategy,
)
from vibe_trader.signals.strategy_lite import SignalSuggest, decide_action


@dataclass
class RuleStrategy:
    """Wrap `strategy_lite.decide_action` as a `Strategy`.

    Knobs map 1:1 to `decide_action` keyword arguments. Defaults are kept
    in sync with `decide_action` to preserve historical behaviour.
    """

    name: str = "rule"
    max_position_per_symbol: int = 200
    critical_size: int = 100
    warn_size: int = 50
    rsi_extreme: float = 30.0

    def decide(self, ctx: StrategyContext) -> SignalSuggest | None:
        """Delegate to the original rule matrix."""
        return decide_action(
            ctx.signals,
            current_qty=ctx.position_qty,
            max_position_per_symbol=self.max_position_per_symbol,
            critical_size=self.critical_size,
            warn_size=self.warn_size,
            rsi_extreme=self.rsi_extreme,
        )


# Idempotent (re-)registration. tests sometimes import this module multiple
# times via `importlib.reload`; the registry's built-in duplicate guard would
# otherwise raise.
def _build_rule(config: dict[str, Any]) -> Strategy:
    return RuleStrategy(**config)


try:
    register_strategy("rule")(_build_rule)
except ValueError:
    # already registered (e.g. test reload) — fine.
    pass
