"""Integration tests wiring Fundamentals → StrategyContext → LLM prompt.

These tests don't exercise a real LLM — they intercept the messages
LLMStrategy would have sent and assert that:

  * fundamentals_md actually appears in the user message;
  * the earnings-blackout warning fires when the snapshot puts us inside
    the window (today derived from ctx.snapshot.update_time);
  * `_run_strategy_per_code` calls FundamentalsClient.fetch once per code
    and tolerates a None result without crashing.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from vibe_trader.data.fundamentals import (
    FixtureFundamentalsClient,
    Fundamentals,
    parse_raw_fundamentals,
)
from vibe_trader.futu_client import Snapshot
from vibe_trader.llm.client import LLMResponse
from vibe_trader.scheduler.jobs import _run_strategy_per_code
from vibe_trader.signals.base import Severity, Signal
from vibe_trader.signals.strategy_base import StrategyContext
from vibe_trader.signals.strategy_llm import LLMStrategy
from vibe_trader.signals.strategy_rule import RuleStrategy


def _fund_for_test(earnings: date | None = date(2026, 5, 21)) -> Fundamentals:
    return parse_raw_fundamentals(
        {
            "ticker": "TEST",
            "code": "US.TEST",
            "fetched_at": "2026-05-07T14:00:00+00:00",
            "info": {
                "currentPrice": 100.0,
                "targetMeanPrice": 130.0,
                "targetHighPrice": 150.0,
                "targetLowPrice": 80.0,
                "targetMedianPrice": 125.0,
                "recommendationKey": "buy",
                "recommendationMean": 1.9,
                "numberOfAnalystOpinions": 30,
            },
            "recommendations": [
                {"index": 0, "period": "0m", "strongBuy": 5, "buy": 20,
                 "hold": 4, "sell": 1, "strongSell": 0},
            ],
            "upgrades_downgrades": [
                {
                    "GradeDate": "2026-05-01T10:00:00",
                    "Firm": "Demo Bank",
                    "ToGrade": "Buy",
                    "FromGrade": "Hold",
                    "Action": "up",
                    "currentPriceTarget": 150.0,
                    "priorPriceTarget": 130.0,
                },
            ],
            "news": [
                {
                    "id": "n1",
                    "content": {
                        "title": "Test news headline",
                        "summary": "Demo summary",
                        "pubDate": "2026-05-06T18:00:00Z",
                        "provider": {"displayName": "Yahoo Finance"},
                        "canonicalUrl": {"url": "https://example.com"},
                    },
                },
            ],
            "calendar": (
                {"Earnings Date": [earnings.isoformat()],
                 "Earnings Average": 1.75}
                if earnings is not None
                else {}
            ),
        }
    )


class _CapturingClient:
    """LLM client double — records the messages and replies HOLD."""

    name = "capturing"
    model = "fake"

    def __init__(self) -> None:
        self.messages: list[list[dict[str, str]]] | None = None

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        temperature: float,
        timeout_s: float,
    ) -> LLMResponse:
        self.messages = list(messages)
        return LLMResponse(
            text='{"action":"HOLD","qty":0,"confidence":0.9,"reason":"test"}',
            prompt_tokens=10,
            completion_tokens=5,
        )


def _make_signal(code: str, ts: datetime | None = None) -> Signal:
    return Signal(
        code=code,
        ts=ts or datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc),
        signal_type="rsi_overbought",
        severity=Severity.WARN,
        payload={"rsi": 72.5},
    )


def _make_snapshot(when: datetime) -> Snapshot:
    return Snapshot(
        code="US.TEST",
        last_price=100.0,
        open_price=99.0,
        high_price=101.0,
        low_price=98.0,
        volume=1_000_000,
        turnover=1.0e8,
        update_time=when,
    )


def _make_llm(client: _CapturingClient, audit_path: Path, dev_path: Path,
              profile: Any | None = None) -> LLMStrategy:
    return LLMStrategy(
        client=client,
        fallback=RuleStrategy(),
        audit_log_path=audit_path,
        dev_log_path=dev_path,
        cache_seconds=0,  # disable cache so every test sees a fresh prompt
        investment_profile=profile,
        fundamentals_max_changes=3,
        fundamentals_max_news=3,
    )


def test_llm_prompt_contains_fundamentals_block(tmp_path: Path) -> None:
    client = _CapturingClient()
    strat = _make_llm(client, tmp_path / "audit.jsonl", tmp_path / "dev.md")
    ctx = StrategyContext(
        code="US.TEST",
        signals=[_make_signal("US.TEST")],
        position_qty=0,
        snapshot=_make_snapshot(datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc)),
        fundamentals=_fund_for_test(),
    )
    strat.decide(ctx)
    assert client.messages is not None, "LLM was not called"
    user_msg = client.messages[-1]["content"]
    assert "Fundamentals (snapshot" in user_msg
    assert "Wall Street consensus" in user_msg
    assert "Test news headline" in user_msg
    assert "Next earnings" in user_msg


def test_llm_prompt_omits_block_when_no_fundamentals(tmp_path: Path) -> None:
    client = _CapturingClient()
    strat = _make_llm(client, tmp_path / "audit.jsonl", tmp_path / "dev.md")
    ctx = StrategyContext(
        code="US.TEST",
        signals=[_make_signal("US.TEST")],
        position_qty=0,
        snapshot=_make_snapshot(datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc)),
        fundamentals=None,
    )
    strat.decide(ctx)
    user_msg = client.messages[-1]["content"]
    assert "Fundamentals (snapshot" not in user_msg
    assert "Wall Street consensus" not in user_msg


class _Profile:
    """Minimal stub matching InvestmentProfileConfig duck-type used by LLMStrategy."""

    enabled = True
    horizon_months_min = 3
    horizon_months_max = 6
    style = "growth"
    theme = "Test thesis"
    budget_per_symbol_usd = 50_000.0
    drawdown_tolerance_pct = 20.0
    max_concentration_pct = 60.0
    initial_entry_pct = 40.0
    max_batches = 3
    add_on_dip_pct = 5.0
    add_cooldown_days = 5
    prefer_dip_buy = True
    take_profit_pct = 30.0
    take_profit_trim_pct = 50.0
    hard_stop_pct = 20.0
    min_holding_days = 30
    earnings_blackout_days = 3


def test_blackout_warning_when_snapshot_inside_window(tmp_path: Path) -> None:
    client = _CapturingClient()
    strat = _make_llm(
        client, tmp_path / "audit.jsonl", tmp_path / "dev.md", profile=_Profile()
    )
    ctx = StrategyContext(
        code="US.TEST",
        signals=[_make_signal("US.TEST")],
        position_qty=0,
        # 2026-05-19, earnings 2026-05-21 → 2 days out (within 3-day blackout)
        snapshot=_make_snapshot(datetime(2026, 5, 19, 14, 0, tzinfo=timezone.utc)),
        fundamentals=_fund_for_test(),
    )
    strat.decide(ctx)
    user_msg = client.messages[-1]["content"]
    assert "WARNING" in user_msg
    assert "Decline new BUY" in user_msg


def test_no_blackout_warning_when_snapshot_outside_window(tmp_path: Path) -> None:
    client = _CapturingClient()
    strat = _make_llm(
        client, tmp_path / "audit.jsonl", tmp_path / "dev.md", profile=_Profile()
    )
    ctx = StrategyContext(
        code="US.TEST",
        signals=[_make_signal("US.TEST")],
        position_qty=0,
        # 14 days out — well clear of the 3-day blackout
        snapshot=_make_snapshot(datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc)),
        fundamentals=_fund_for_test(),
    )
    strat.decide(ctx)
    user_msg = client.messages[-1]["content"]
    assert "WARNING" not in user_msg


# ---------------------------------------------------------------------------
# scheduler wiring: _run_strategy_per_code injects Fundamentals via the client.
# ---------------------------------------------------------------------------


class _RecordingClient:
    """FundamentalsClient stub recording every fetch() call."""

    name = "recording"

    def __init__(self, returns: dict[str, Fundamentals | None]) -> None:
        self.returns = returns
        self.calls: list[str] = []

    def fetch(self, code: str) -> Fundamentals | None:
        self.calls.append(code)
        return self.returns.get(code)


class _AssertingStrategy:
    """Strategy that records the StrategyContext.fundamentals it sees."""

    name = "asserting"

    def __init__(self) -> None:
        self.seen: dict[str, Fundamentals | None] = {}

    def decide(self, ctx: StrategyContext):
        self.seen[ctx.code] = ctx.fundamentals
        return None


def test_run_strategy_passes_fundamentals_through_client() -> None:
    fund_a = _fund_for_test()
    client = _RecordingClient({"US.A": fund_a})  # B is missing → None
    strat = _AssertingStrategy()
    sigs_by_code = {
        "US.A": [_make_signal("US.A")],
        "US.B": [_make_signal("US.B")],
    }
    _run_strategy_per_code(
        strat, sigs_by_code, positions={}, fundamentals_client=client
    )
    assert sorted(client.calls) == ["US.A", "US.B"]
    assert strat.seen["US.A"] is fund_a
    assert strat.seen["US.B"] is None


def test_run_strategy_swallows_fundamentals_lookup_error() -> None:
    class _BoomClient:
        name = "boom"

        def fetch(self, code: str) -> Fundamentals | None:
            raise RuntimeError("disk on fire")

    strat = _AssertingStrategy()
    sigs_by_code = {"US.A": [_make_signal("US.A")]}
    _run_strategy_per_code(
        strat, sigs_by_code, positions={}, fundamentals_client=_BoomClient()
    )
    assert strat.seen["US.A"] is None


def test_fixture_client_via_factory(tmp_path: Path) -> None:
    """End-to-end: factory → FixtureFundamentalsClient → fetched dataclass."""
    import json

    payload = {
        "ticker": "TEST",
        "code": "US.TEST",
        "fetched_at": "2026-05-07T14:00:00+00:00",
        "info": {"currentPrice": 100.0, "targetMeanPrice": 110.0,
                 "recommendationKey": "buy", "numberOfAnalystOpinions": 1},
        "recommendations": [],
        "upgrades_downgrades": [],
        "news": [],
        "calendar": {},
    }
    (tmp_path / "US.TEST.json").write_text(json.dumps(payload))
    client = FixtureFundamentalsClient(root=tmp_path)
    fund = client.fetch("US.TEST")
    assert fund is not None
    assert fund.consensus.recommendation_key == "buy"
