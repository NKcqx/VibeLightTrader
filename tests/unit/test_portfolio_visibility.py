"""Portfolio-aware prompt visibility (no hard guards — Plan A).

Covers the four moving parts:
  1. ``OpenDSecTrader.query_account`` parses the Futu accinfo row.
  2. ``FakePaperTrader.query_account`` returns a deterministic snapshot.
  3. ``_compute_portfolio_state`` builds a PortfolioSnapshot from the
     broker view + position table + live snapshots.
  4. ``render_user_prompt`` shows the Portfolio block (and the
     CONCENTRATION CAP / LOW CASH callouts) when the snapshot is present.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pandas as pd
import pytest

from vibe_trader.llm.prompt import render_user_prompt
from vibe_trader.scheduler.jobs import _compute_portfolio_state
from vibe_trader.signals.strategy_base import PortfolioSnapshot
from vibe_trader.trader.paper import FakePaperTrader, PaperAccount


# ---------------------------------------------------------------------------
# FakePaperTrader.query_account
# ---------------------------------------------------------------------------


def test_fake_query_account_basic() -> None:
    fake = FakePaperTrader(cash=100_000.0)
    acct = fake.query_account()
    assert acct.cash == 100_000.0
    assert acct.market_val == 0.0
    assert acct.total_assets == 100_000.0
    assert acct.currency == "USD"


def test_fake_query_account_with_marked_position() -> None:
    fake = FakePaperTrader(cash=50_000.0)
    fake.set_mark("US.NVDA", 200.0)
    fake.place_order(code="US.NVDA", side="BUY", qty=100, order_type="MARKET")
    acct = fake.query_account()
    # Cash isn't auto-debited in the fake (test broker is pure-broker;
    # cash flow modelling lives elsewhere). market_val should reflect
    # the new position × mark.
    assert acct.market_val == 100 * 200.0
    assert acct.cash == 50_000.0  # unchanged: fake doesn't debit
    assert acct.total_assets == acct.cash + acct.market_val


# ---------------------------------------------------------------------------
# _compute_portfolio_state
# ---------------------------------------------------------------------------


@dataclass
class _Snap:
    last_price: float


def test_compute_portfolio_state_returns_none_when_no_broker() -> None:
    out = _compute_portfolio_state(
        paper_trader=None, position_details={}, snapshots_by_code={}
    )
    assert out is None


def test_compute_portfolio_state_returns_none_when_account_query_fails() -> None:
    broker = MagicMock()
    broker.query_account.side_effect = RuntimeError("OpenD timeout")
    out = _compute_portfolio_state(
        paper_trader=broker, position_details={}, snapshots_by_code={}
    )
    assert out is None


def test_compute_portfolio_state_aggregates_holdings() -> None:
    broker = MagicMock()
    broker.query_account.return_value = PaperAccount(
        cash=950_000.0, market_val=53_000.0, total_assets=1_003_000.0,
        buying_power=1_953_000.0, currency="USD",
    )
    out = _compute_portfolio_state(
        paper_trader=broker,
        position_details={
            "US.NVDA": (250, 200.0, 0.0),
            "US.MSFT": (0, 0.0, 0.0),  # closed: should be skipped
        },
        snapshots_by_code={"US.NVDA": _Snap(last_price=212.5)},
    )
    assert isinstance(out, PortfolioSnapshot)
    # NVDA value uses live mark, not avg_cost.
    assert out.holdings == {"US.NVDA": 250 * 212.5}
    assert "US.MSFT" not in out.holdings  # closed positions filtered
    assert out.cash == 950_000.0
    assert out.market_val == 53_000.0
    assert pytest.approx(out.invested_pct, rel=1e-3) == 53_000.0 / 1_003_000.0 * 100
    assert pytest.approx(out.cash_pct, rel=1e-3) == 950_000.0 / 1_003_000.0 * 100
    assert out.buying_power == 1_953_000.0


def test_compute_portfolio_state_falls_back_to_avg_cost_when_snapshot_missing() -> None:
    broker = MagicMock()
    broker.query_account.return_value = PaperAccount(
        cash=100.0, market_val=20_000.0, total_assets=20_100.0
    )
    out = _compute_portfolio_state(
        paper_trader=broker,
        position_details={"US.NVDA": (100, 200.0, 0.0)},
        snapshots_by_code={},  # no snapshot
    )
    assert out is not None
    assert out.holdings == {"US.NVDA": 100 * 200.0}  # avg_cost fallback


def test_compute_portfolio_state_sorts_holdings_desc() -> None:
    broker = MagicMock()
    broker.query_account.return_value = PaperAccount(
        cash=100.0, market_val=100_000.0, total_assets=100_100.0
    )
    out = _compute_portfolio_state(
        paper_trader=broker,
        position_details={
            "US.A": (10, 100.0, 0.0),    # 1k
            "US.B": (50, 100.0, 0.0),    # 5k
            "US.C": (200, 100.0, 0.0),   # 20k
        },
        snapshots_by_code={
            "US.A": _Snap(100.0), "US.B": _Snap(100.0), "US.C": _Snap(100.0),
        },
    )
    codes = list(out.holdings.keys())
    assert codes == ["US.C", "US.B", "US.A"]


# ---------------------------------------------------------------------------
# render_user_prompt: Portfolio block + callouts.
# ---------------------------------------------------------------------------


@dataclass
class _Profile:
    enabled: bool = True
    horizon_months_min: int = 3
    horizon_months_max: int = 6
    style: str = "growth"
    theme: str = "Tech"
    budget_per_symbol_usd: float = 50_000.0
    drawdown_tolerance_pct: float = 20
    max_concentration_pct: float = 30
    cash_reserve_pct: float = 15
    initial_entry_pct: float = 40
    max_batches: int = 3
    add_on_dip_pct: float = 5
    add_cooldown_days: int = 5
    prefer_dip_buy: bool = True
    take_profit_pct: float = 30
    take_profit_trim_pct: float = 50
    hard_stop_pct: float = 20
    min_holding_days: int = 30


def _make_ps(**kw: Any) -> PortfolioSnapshot:
    base = dict(
        cash=900_000.0, market_val=100_000.0, total_assets=1_000_000.0,
        invested_pct=10.0, cash_pct=90.0,
        holdings={"US.NVDA": 53_000.0, "US.MSFT": 47_000.0},
        holdings_pct={"US.NVDA": 5.3, "US.MSFT": 4.7},
        buying_power=1_953_000.0, currency="USD",
    )
    base.update(kw)
    return PortfolioSnapshot(**base)


def _render(**kw: Any) -> str:
    base = dict(
        code="US.AVGO", snapshot=None, position_qty=0, avg_cost=0.0,
        realized_pnl=0.0, intraday_return=None, last_30_bar_return=None,
        indicators=None, signals=[], max_position=200, min_trade_size=10,
        min_confidence=0.6,
    )
    base.update(kw)
    return render_user_prompt(**base)


def test_prompt_omits_portfolio_block_when_none() -> None:
    out = _render(profile=_Profile())  # no portfolio
    assert "Portfolio (account-wide" not in out
    assert "this_symbol:" not in out


def test_prompt_renders_portfolio_block() -> None:
    out = _render(profile=_Profile(), portfolio=_make_ps())
    assert "Portfolio (account-wide" in out
    assert "share this cash pool" in out
    assert "$900,000" in out          # cash, formatted with thousand-sep
    assert "$100,000" in out          # invested
    assert "$1,000,000 USD" in out    # total_assets
    assert "buying_power:" in out
    assert "this_symbol:    $0" in out  # AVGO not held
    assert "other holdings:" in out
    assert "US.NVDA: $53,000" in out
    assert "US.MSFT: $47,000" in out


def test_prompt_concentration_cap_callout_when_over_threshold() -> None:
    """When the *current* symbol is already at/over max_concentration_pct."""
    ps = _make_ps(
        holdings={"US.AVGO": 350_000.0},
        holdings_pct={"US.AVGO": 35.0},  # over 30% cap
    )
    out = _render(code="US.AVGO", profile=_Profile(), portfolio=ps)
    assert "CONCENTRATION CAP" in out


def test_prompt_low_cash_callout_when_below_reserve() -> None:
    ps = _make_ps(
        cash=120_000.0, market_val=880_000.0, total_assets=1_000_000.0,
        cash_pct=12.0, invested_pct=88.0,
    )
    out = _render(profile=_Profile(), portfolio=ps)
    assert "LOW CASH" in out
    assert "12.0%" in out


def test_prompt_no_callouts_when_within_limits() -> None:
    out = _render(profile=_Profile(), portfolio=_make_ps())
    assert "CONCENTRATION CAP" not in out
    assert "LOW CASH" not in out


def test_prompt_other_holdings_excludes_current_symbol() -> None:
    ps = _make_ps(
        holdings={"US.NVDA": 53_000.0, "US.MSFT": 47_000.0},
        holdings_pct={"US.NVDA": 5.3, "US.MSFT": 4.7},
    )
    out = _render(code="US.NVDA", profile=_Profile(), portfolio=ps)
    # NVDA appears in this_symbol line, not in 'other holdings' list.
    other_section = out.split("other holdings:")[-1]
    assert "US.MSFT" in other_section
    assert "US.NVDA" not in other_section
