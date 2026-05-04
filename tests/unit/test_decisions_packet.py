"""Unit tests for decisions/packet.py — build_packet + render_packet_md.

These cover the format invariants the rest of the HITL pipeline relies on:
the receiver instructions are present, hard constraints are quoted, and
the markdown is a valid superset of all required sections.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pandas as pd
import pytest

from equity_monitor.decisions.packet import (
    DecisionPacket,
    build_packet,
    default_memory_hints,
    make_packet_id,
    packet_to_json,
    render_packet_md,
)
from equity_monitor.signals.base import Severity, Signal
from equity_monitor.signals.strategy_base import StrategyContext


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


def _sig(stype: str = "rsi_oversold", payload: dict | None = None) -> Signal:
    return Signal(
        code="US.NVDA",
        ts=datetime(2026, 5, 4, 14, tzinfo=timezone.utc),
        signal_type=stype,
        severity=Severity.WARN,
        payload=payload or {"rsi": 28.5, "close": 850.0},
    )


def _kline_df() -> pd.DataFrame:
    """Mini kline df with all indicator columns populated for the last bar."""
    idx = pd.date_range("2026-05-04 09:00", periods=3, freq="60min")
    return pd.DataFrame(
        {
            "close": [840.0, 845.0, 850.0],
            "rsi_14": [None, 32.0, 28.5],
            "macd": [None, -1.2, -0.8],
            "macd_signal": [None, -1.0, -0.6],
            "macd_hist": [None, -0.2, -0.2],
            "boll_upper": [None, 870.0, 872.0],
            "boll_mid": [None, 850.0, 852.0],
            "boll_lower": [None, 830.0, 832.0],
        },
        index=idx,
    )


def _ctx(*, with_kline: bool = True, with_snapshot: bool = True) -> StrategyContext:
    snap = None
    if with_snapshot:
        snap = MagicMock()
        snap.last_price = 850.42
        snap.open_price = 845.0
        snap.prev_close_price = 840.0
        snap.high_price = 855.0
        snap.low_price = 838.0
        snap.volume = 12_345_678
        snap.ts = datetime(2026, 5, 4, 14, tzinfo=timezone.utc)
    return StrategyContext(
        code="US.NVDA",
        signals=[_sig()],
        position_qty=50,
        snapshot=snap,
        kline_60m=_kline_df() if with_kline else None,
        avg_cost=820.0,
        realized_pnl=345.67,
        intraday_return=0.012,
        last_30_bar_return=-0.025,
    )


def _constraints() -> dict[str, Any]:
    return {"max_position": 200, "min_trade_size": 10, "min_confidence": 0.6}


# ---------------------------------------------------------------------------
# make_packet_id — sortable + collision-resistant.
# ---------------------------------------------------------------------------


def test_packet_id_format_sortable() -> None:
    a = make_packet_id(datetime(2026, 5, 4, 9, 0, tzinfo=timezone.utc))
    b = make_packet_id(datetime(2026, 5, 4, 10, 0, tzinfo=timezone.utc))
    assert a < b, "ids generated later must sort after earlier ones"
    assert a.startswith("20260504T090000Z_")


def test_packet_ids_unique_in_same_second() -> None:
    now = datetime(2026, 5, 4, tzinfo=timezone.utc)
    seen = {make_packet_id(now) for _ in range(100)}
    assert len(seen) == 100  # uuid suffix prevents collisions


# ---------------------------------------------------------------------------
# build_packet — context → packet.
# ---------------------------------------------------------------------------


def test_build_packet_extracts_indicators_from_last_bar() -> None:
    p = build_packet(
        _ctx(),
        triggering_signal_ids=[42, 43],
        constraints=_constraints(),
    )
    assert p.code == "US.NVDA"
    assert p.position_qty == 50
    assert p.avg_cost == 820.0
    assert p.realized_pnl == 345.67
    assert p.intraday_return == 0.012
    assert p.last_30_bar_return == -0.025
    assert p.triggering_signal_ids == [42, 43]
    assert p.triggering_signal_types == ["rsi_oversold"]
    assert p.indicators is not None
    assert p.indicators["rsi_14"] == 28.5
    assert p.indicators["boll_upper"] == 872.0


def test_build_packet_handles_missing_kline_gracefully() -> None:
    p = build_packet(
        _ctx(with_kline=False),
        triggering_signal_ids=[],
        constraints=_constraints(),
    )
    assert p.indicators is None  # no kline → no indicators block


def test_build_packet_handles_missing_snapshot() -> None:
    p = build_packet(
        _ctx(with_snapshot=False),
        triggering_signal_ids=[],
        constraints=_constraints(),
    )
    assert p.snapshot is None
    # Still renders without crashing
    md = render_packet_md(p)
    assert "无实时快照" in md


def test_build_packet_serialises_signal_payload() -> None:
    p = build_packet(_ctx(), triggering_signal_ids=[], constraints=_constraints())
    assert len(p.signals) == 1
    s0 = p.signals[0]
    assert s0["signal_type"] == "rsi_oversold"
    assert s0["severity"] == "WARN"
    assert s0["payload"] == {"rsi": 28.5, "close": 850.0}


def test_packet_to_json_roundtrip() -> None:
    """Serialised packet must JSON-decode without loss for replay."""
    import json

    p = build_packet(_ctx(), triggering_signal_ids=[], constraints=_constraints())
    s = packet_to_json(p)
    data = json.loads(s)
    assert data["code"] == "US.NVDA"
    assert data["position_qty"] == 50


# ---------------------------------------------------------------------------
# render_packet_md — the heart of self-dialogue.
# ---------------------------------------------------------------------------


def test_render_includes_self_dialogue_header() -> None:
    p = build_packet(_ctx(), triggering_signal_ids=[], constraints=_constraints())
    md = render_packet_md(p)
    # The receiver-instruction header is the whole point of HITL — it
    # *commands* the receiving Claude to recall MEMORY before deciding.
    assert "致 Claude" in md
    assert "self-instructions" in md or "self-instruction" in md.lower()
    assert "MEMORY" in md


def test_render_lists_memory_hints() -> None:
    p = build_packet(
        _ctx(),
        triggering_signal_ids=[],
        constraints=_constraints(),
        memory_hints=["rg foo /tmp/bar", "Read /tmp/baz.md"],
    )
    md = render_packet_md(p)
    assert "rg foo /tmp/bar" in md
    assert "Read /tmp/baz.md" in md


def test_render_quotes_hard_constraints() -> None:
    p = build_packet(
        _ctx(),
        triggering_signal_ids=[],
        constraints={"max_position": 200, "min_trade_size": 10, "min_confidence": 0.6},
    )
    md = render_packet_md(p)
    assert "max_position" in md
    assert "200" in md
    assert "min_confidence" in md
    assert "0.6" in md


def test_render_includes_output_schema() -> None:
    p = build_packet(_ctx(), triggering_signal_ids=[], constraints=_constraints())
    md = render_packet_md(p)
    assert '"action"' in md
    assert "BUY|SELL|HOLD" in md
    assert "memory_used" in md  # proof-of-recall field


def test_render_json_schema_uses_single_braces() -> None:
    """Regression: an earlier version had `{{` / `}}` leftover from
    Python `.format()` escaping, which would render literally and trip
    up the receiving Claude when it tries to copy/parse the schema."""
    p = build_packet(_ctx(), triggering_signal_ids=[], constraints=_constraints())
    md = render_packet_md(p)
    assert "{{" not in md
    assert "}}" not in md


def test_render_includes_submit_command_with_packet_id() -> None:
    p = build_packet(_ctx(), triggering_signal_ids=[], constraints=_constraints())
    md = render_packet_md(p)
    assert "equity-monitor decide submit" in md
    assert p.id in md


def test_render_handles_n_a_for_missing_intraday() -> None:
    ctx = StrategyContext(
        code="US.NVDA",
        signals=[_sig()],
        position_qty=0,
        snapshot=None,
        kline_60m=None,
        avg_cost=0.0,
        realized_pnl=0.0,
        intraday_return=None,
        last_30_bar_return=None,
    )
    p = build_packet(ctx, triggering_signal_ids=[], constraints=_constraints())
    md = render_packet_md(p)
    # Must not crash on Nones; "n/a" placeholders appear instead
    assert "n/a" in md


def test_render_uses_repo_root_in_submit_path() -> None:
    p = build_packet(_ctx(), triggering_signal_ids=[], constraints=_constraints())
    md = render_packet_md(p, repo_root=Path("/abs/path/to/repo"))
    assert "/abs/path/to/repo" in md
    assert "/abs/path/to/repo/var/decisions/submitted/" in md


# ---------------------------------------------------------------------------
# default_memory_hints — sanity that they include the four key probes.
# ---------------------------------------------------------------------------


def test_default_memory_hints_cover_key_sources() -> None:
    hints = default_memory_hints(code="US.NVDA")
    blob = "\n".join(hints)
    assert "transcripts" in blob.lower()  # transcript grep
    assert "README.md" in blob  # readme reference
    assert "llm_decisions.jsonl" in blob  # prior audit
    assert "US.NVDA" in blob  # symbol-specific trade history
