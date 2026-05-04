"""Unit tests for HITLStrategy.

Verifies the contract: decide() ALWAYS returns None, ALWAYS produces a
pending packet, and (best-effort) pushes a Lark summary.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from equity_monitor.decisions.store import PacketState, PacketStore
from equity_monitor.signals.base import Severity, Signal
from equity_monitor.signals.strategy_base import StrategyContext
from equity_monitor.signals.strategy_hitl import HITLStrategy


def _sig(stype: str = "rsi_oversold") -> Signal:
    return Signal(
        code="US.NVDA",
        ts=datetime(2026, 5, 4, tzinfo=timezone.utc),
        signal_type=stype,
        severity=Severity.WARN,
        payload={"rsi": 28.0, "close": 850.0},
    )


def _ctx(*, signals: list[Signal] | None = None) -> StrategyContext:
    return StrategyContext(
        code="US.NVDA",
        signals=signals if signals is not None else [_sig()],
        position_qty=50,
        avg_cost=820.0,
        realized_pnl=0.0,
        intraday_return=-0.005,
    )


def test_returns_none_for_empty_signals(tmp_path: Path) -> None:
    store = PacketStore(tmp_path)
    strat = HITLStrategy(store=store)
    assert strat.decide(_ctx(signals=[])) is None
    # No packet produced
    assert list(store.list(state=PacketState.PENDING)) == []


def test_returns_none_with_signals_and_writes_pending(tmp_path: Path) -> None:
    store = PacketStore(tmp_path)
    strat = HITLStrategy(store=store)
    out = strat.decide(_ctx())
    assert out is None  # HITL never auto-trades
    pendings = list(store.list(state=PacketState.PENDING))
    assert len(pendings) == 1
    p = pendings[0].packet
    assert p.code == "US.NVDA"
    assert p.position_qty == 50
    assert p.triggering_signal_types == ["rsi_oversold"]


def test_packet_carries_constraint_block(tmp_path: Path) -> None:
    store = PacketStore(tmp_path)
    strat = HITLStrategy(
        store=store, max_position=300, min_trade_size=20, min_confidence=0.7
    )
    strat.decide(_ctx())
    sp = next(iter(store.list(state=PacketState.PENDING)))
    assert sp.packet.constraints == {
        "max_position": 300,
        "min_trade_size": 20,
        "min_confidence": 0.7,
    }


def test_lark_push_called_with_summary(tmp_path: Path) -> None:
    captured: list[str] = []

    def fake_push(body: str) -> str:
        captured.append(body)
        return "msg_id_xyz"

    store = PacketStore(tmp_path)
    strat = HITLStrategy(store=store, lark_push=fake_push)
    strat.decide(_ctx())
    assert len(captured) == 1
    body = captured[0]
    assert "HITL 决策待办" in body
    assert "US.NVDA" in body
    assert "equity-monitor decide submit" in body
    # The packet id appears in both the submit command and the suggested
    # write-path; just sanity-check it's there
    pending = next(iter(store.list(state=PacketState.PENDING)))
    assert pending.packet.id in body


def test_lark_push_failure_doesnt_kill_packet(tmp_path: Path) -> None:
    """A flaky Lark API must NOT lose the packet — it's already on disk."""

    def boom_push(body: str) -> str:
        raise RuntimeError("Lark gateway down")

    store = PacketStore(tmp_path)
    strat = HITLStrategy(store=store, lark_push=boom_push)
    out = strat.decide(_ctx())
    assert out is None
    # Packet still exists in pending despite push failure
    assert len(list(store.list(state=PacketState.PENDING))) == 1


def test_extra_memory_hints_appended(tmp_path: Path) -> None:
    store = PacketStore(tmp_path)
    strat = HITLStrategy(
        store=store,
        extra_memory_hints=["# custom: read foo.md", "rg bar /tmp/baz"],
    )
    strat.decide(_ctx())
    sp = next(iter(store.list(state=PacketState.PENDING)))
    md = sp.markdown()
    assert "# custom: read foo.md" in md
    assert "rg bar /tmp/baz" in md
