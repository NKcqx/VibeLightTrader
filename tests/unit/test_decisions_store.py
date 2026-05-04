"""Unit tests for decisions/store.py — filesystem state machine.

Validates the four-state lifecycle (pending → submitted → executed |
cancelled), schema validation on submit, and round-trip read/write.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from equity_monitor.decisions.packet import DecisionPacket, build_packet
from equity_monitor.decisions.store import PacketState, PacketStore
from equity_monitor.signals.base import Severity, Signal
from equity_monitor.signals.strategy_base import StrategyContext


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


def _ctx() -> StrategyContext:
    sig = Signal(
        code="US.NVDA",
        ts=datetime(2026, 5, 4, tzinfo=timezone.utc),
        signal_type="rsi_oversold",
        severity=Severity.WARN,
        payload={"rsi": 28.0},
    )
    return StrategyContext(
        code="US.NVDA",
        signals=[sig],
        position_qty=50,
        avg_cost=820.0,
        realized_pnl=0.0,
    )


def _packet() -> DecisionPacket:
    return build_packet(
        _ctx(),
        triggering_signal_ids=[],
        constraints={"max_position": 200, "min_trade_size": 10, "min_confidence": 0.6},
    )


# ---------------------------------------------------------------------------
# Lifecycle.
# ---------------------------------------------------------------------------


def test_write_pending_creates_md_and_json(tmp_path: Path) -> None:
    store = PacketStore(tmp_path)
    p = _packet()
    sp = store.write_pending(p)

    assert sp.state == PacketState.PENDING
    assert sp.md_path.exists()
    assert sp.json_path.exists()
    md = sp.markdown()
    assert p.code in md
    assert p.id in md


def test_get_finds_packet_in_any_state(tmp_path: Path) -> None:
    store = PacketStore(tmp_path)
    p = _packet()
    store.write_pending(p)
    found = store.get(p.id)
    assert found is not None
    assert found.state == PacketState.PENDING


def test_get_returns_none_for_unknown_id(tmp_path: Path) -> None:
    store = PacketStore(tmp_path)
    assert store.get("nonexistent_id") is None


def test_submit_pending_to_submitted(tmp_path: Path) -> None:
    store = PacketStore(tmp_path)
    p = _packet()
    store.write_pending(p)

    decision = {
        "action": "BUY",
        "qty": 50,
        "confidence": 0.8,
        "reason": "RSI 超卖反弹机会",
    }
    sp2 = store.submit(p.id, decision)

    assert sp2.state == PacketState.SUBMITTED
    assert sp2.decision == decision
    # Old pending files gone
    pending_md = tmp_path / "pending" / f"{p.id}.md"
    assert not pending_md.exists()


def test_submit_rejects_missing_required_fields(tmp_path: Path) -> None:
    store = PacketStore(tmp_path)
    p = _packet()
    store.write_pending(p)
    with pytest.raises(ValueError, match="missing required fields"):
        store.submit(
            p.id,
            {"action": "BUY"},  # missing qty/confidence/reason
        )


def test_submit_unknown_packet_raises(tmp_path: Path) -> None:
    store = PacketStore(tmp_path)
    with pytest.raises(FileNotFoundError):
        store.submit(
            "no_such_id",
            {"action": "HOLD", "qty": 0, "confidence": 0.5, "reason": "x"},
        )


def test_mark_executed_records_outcome(tmp_path: Path) -> None:
    store = PacketStore(tmp_path)
    p = _packet()
    store.write_pending(p)
    store.submit(
        p.id,
        {"action": "BUY", "qty": 50, "confidence": 0.8, "reason": "ok"},
    )
    sp = store.mark_executed(
        p.id,
        execution={
            "status": "FILLED",
            "trade_id": 99,
            "side": "BUY",
            "qty": 50,
        },
    )
    assert sp.state == PacketState.EXECUTED
    assert sp.execution["trade_id"] == 99


def test_cancel_from_pending(tmp_path: Path) -> None:
    store = PacketStore(tmp_path)
    p = _packet()
    store.write_pending(p)
    sp = store.cancel(p.id, reason="user-changed-mind")
    assert sp.state == PacketState.CANCELLED


def test_cancel_from_submitted(tmp_path: Path) -> None:
    store = PacketStore(tmp_path)
    p = _packet()
    store.write_pending(p)
    store.submit(
        p.id,
        {"action": "BUY", "qty": 10, "confidence": 0.9, "reason": "x"},
    )
    sp = store.cancel(p.id, reason="changed-mind-after-submit")
    assert sp.state == PacketState.CANCELLED


def test_cannot_cancel_executed_packet(tmp_path: Path) -> None:
    store = PacketStore(tmp_path)
    p = _packet()
    store.write_pending(p)
    store.submit(
        p.id,
        {"action": "BUY", "qty": 10, "confidence": 0.9, "reason": "x"},
    )
    store.mark_executed(p.id, execution={"status": "FILLED"})
    with pytest.raises(FileNotFoundError):
        store.cancel(p.id)


# ---------------------------------------------------------------------------
# Listing — chronological order.
# ---------------------------------------------------------------------------


def test_list_returns_chronological_order(tmp_path: Path) -> None:
    """Packet ids embed a timestamp prefix → lexicographic = chronological."""
    store = PacketStore(tmp_path)
    p1 = build_packet(
        _ctx(),
        triggering_signal_ids=[],
        constraints={"max_position": 200},
        # use explicit ts via packet_id construction for deterministic test
    )
    p2 = build_packet(
        _ctx(),
        triggering_signal_ids=[],
        constraints={"max_position": 200},
    )
    # p1 / p2 IDs will differ by uuid suffix; both same minute; stable ordering only matters
    # in the same logical second. Build explicit ids:
    from equity_monitor.decisions.packet import make_packet_id

    earlier = make_packet_id(datetime(2026, 5, 4, 9, 0, tzinfo=timezone.utc))
    later = make_packet_id(datetime(2026, 5, 4, 10, 0, tzinfo=timezone.utc))

    p_e = build_packet(
        _ctx(), triggering_signal_ids=[], constraints={}, packet_id=earlier
    )
    p_l = build_packet(
        _ctx(), triggering_signal_ids=[], constraints={}, packet_id=later
    )
    store.write_pending(p_l)
    store.write_pending(p_e)

    listed = list(store.list(state=PacketState.PENDING))
    assert [sp.packet.id for sp in listed] == [earlier, later]


def test_list_filters_by_state(tmp_path: Path) -> None:
    store = PacketStore(tmp_path)
    p = _packet()
    store.write_pending(p)

    pending_only = list(store.list(state=PacketState.PENDING))
    assert len(pending_only) == 1
    submitted_only = list(store.list(state=PacketState.SUBMITTED))
    assert submitted_only == []
