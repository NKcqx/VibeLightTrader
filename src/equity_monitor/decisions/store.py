"""Filesystem-backed packet store.

Why filesystem and not a DB table?

  - The user submits decisions either via CLI or by writing the file
    directly (when a Cursor agent has write access). A bare directory
    is the lowest-friction interface for both.
  - Each packet is human-readable markdown — the user can `cat`,
    `less`, or open in any editor.
  - The state of a packet is encoded as which subdirectory it's in,
    so `mv` is atomic on a single filesystem (POSIX rename).

Layout::

    var/decisions/
      pending/      <- created by HITLStrategy; awaiting user
      submitted/    <- user / Claude wrote a response; awaits execution
      executed/     <- terminal: trade placed (or rejected by constraints)
      cancelled/    <- terminal: user cancelled OR ttl-expired

Each packet has two files in its directory at any time:
  <id>.md       human-readable prompt
  <id>.json     machine-readable (DecisionPacket + later, Decision +
                 ExecutionResult layers appended on transitions)
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterator

from equity_monitor.decisions.packet import (
    DecisionPacket,
    packet_to_json,
    render_packet_md,
)


class PacketState(str, Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    EXECUTED = "executed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class StoredPacket:
    """In-memory view of an on-disk packet, lazy: `.markdown()` re-reads."""

    state: PacketState
    packet: DecisionPacket
    md_path: Path
    json_path: Path
    decision: dict[str, Any] | None = None  # set after submit
    execution: dict[str, Any] | None = None  # set after executed/cancelled

    def markdown(self) -> str:
        return self.md_path.read_text(encoding="utf-8")


class PacketStore:
    """Single-root packet store. Thread-safe-enough for our scale (one
    cron writer, occasional CLI reader/writer; SQLite-style coarse
    locking via atomic mv)."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        for s in PacketState:
            (self.root / s.value).mkdir(parents=True, exist_ok=True)

    # -------------------- write paths --------------------

    def write_pending(
        self, p: DecisionPacket, *, repo_root: Path | None = None
    ) -> StoredPacket:
        """Persist a freshly-built packet as PENDING."""
        d = self.root / PacketState.PENDING.value
        md = d / f"{p.id}.md"
        js = d / f"{p.id}.json"
        md.write_text(render_packet_md(p, repo_root=repo_root), encoding="utf-8")
        js.write_text(
            json.dumps(
                {"packet": asdict(p), "state_history": [_event("created")]},
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        return StoredPacket(
            state=PacketState.PENDING, packet=p, md_path=md, json_path=js
        )

    def submit(
        self, packet_id: str, decision: dict[str, Any]
    ) -> StoredPacket:
        """Move pending → submitted; embed the user's decision JSON.

        Raises FileNotFoundError if the packet isn't in pending state.
        Raises ValueError if `decision` is missing required fields.
        """
        _validate_decision_shape(decision)

        src_md, src_js = self._paths(PacketState.PENDING, packet_id)
        if not src_md.exists():
            raise FileNotFoundError(
                f"packet {packet_id!r} not in pending; "
                f"check `equity-monitor decide list`"
            )

        # Append decision to json
        data = json.loads(src_js.read_text(encoding="utf-8"))
        data["decision"] = decision
        data.setdefault("state_history", []).append(_event("submitted"))
        src_js.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

        dst_md, dst_js = self._paths(PacketState.SUBMITTED, packet_id)
        os.replace(src_md, dst_md)
        os.replace(src_js, dst_js)

        return self._load(PacketState.SUBMITTED, packet_id)

    def mark_executed(
        self, packet_id: str, execution: dict[str, Any]
    ) -> StoredPacket:
        """Move submitted → executed; embed execution result.

        `execution` typically: {"trade_id": int, "executed_qty": int,
        "executed_price": float, "status": "FILLED|PENDING|REJECTED",
        "error": str | None}
        """
        return self._terminal_move(
            packet_id,
            from_state=PacketState.SUBMITTED,
            to_state=PacketState.EXECUTED,
            payload_key="execution",
            payload=execution,
        )

    def cancel(
        self, packet_id: str, reason: str = "user-cancelled"
    ) -> StoredPacket:
        """Move pending → cancelled (or submitted → cancelled if user
        cancels after submitting but before execution)."""
        for from_state in (PacketState.PENDING, PacketState.SUBMITTED):
            md, _ = self._paths(from_state, packet_id)
            if md.exists():
                return self._terminal_move(
                    packet_id,
                    from_state=from_state,
                    to_state=PacketState.CANCELLED,
                    payload_key="cancellation",
                    payload={"reason": reason},
                )
        raise FileNotFoundError(
            f"packet {packet_id!r} not in pending or submitted"
        )

    # -------------------- read paths --------------------

    def get(self, packet_id: str) -> StoredPacket | None:
        """Find the packet by id across all states; return None if missing."""
        for s in PacketState:
            md, _ = self._paths(s, packet_id)
            if md.exists():
                return self._load(s, packet_id)
        return None

    def list(
        self, state: PacketState | None = None
    ) -> Iterator[StoredPacket]:
        """List packets in lexicographic id order (= chronological,
        thanks to the timestamp-prefix in `make_packet_id`)."""
        states = [state] if state is not None else list(PacketState)
        for s in states:
            d = self.root / s.value
            for js in sorted(d.glob("*.json")):
                pid = js.stem
                yield self._load(s, pid)

    # -------------------- internals --------------------

    def _paths(self, state: PacketState, packet_id: str) -> tuple[Path, Path]:
        d = self.root / state.value
        return d / f"{packet_id}.md", d / f"{packet_id}.json"

    def _load(self, state: PacketState, packet_id: str) -> StoredPacket:
        md, js = self._paths(state, packet_id)
        data = json.loads(js.read_text(encoding="utf-8"))
        packet_dict = data["packet"]
        # Re-hydrate DecisionPacket; tolerate extra fields from older versions.
        known_fields = {f for f in DecisionPacket.__dataclass_fields__}
        filtered = {k: v for k, v in packet_dict.items() if k in known_fields}
        p = DecisionPacket(**filtered)
        return StoredPacket(
            state=state,
            packet=p,
            md_path=md,
            json_path=js,
            decision=data.get("decision"),
            execution=data.get("execution"),
        )

    def _terminal_move(
        self,
        packet_id: str,
        *,
        from_state: PacketState,
        to_state: PacketState,
        payload_key: str,
        payload: dict[str, Any],
    ) -> StoredPacket:
        src_md, src_js = self._paths(from_state, packet_id)
        if not src_md.exists():
            raise FileNotFoundError(
                f"packet {packet_id!r} not in {from_state.value}"
            )
        data = json.loads(src_js.read_text(encoding="utf-8"))
        data[payload_key] = payload
        data.setdefault("state_history", []).append(_event(to_state.value))
        src_js.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        dst_md, dst_js = self._paths(to_state, packet_id)
        os.replace(src_md, dst_md)
        os.replace(src_js, dst_js)
        return self._load(to_state, packet_id)


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _event(name: str) -> dict[str, str]:
    return {
        "state": name,
        "ts": datetime.now(tz=timezone.utc).isoformat(),
    }


_REQUIRED_DECISION_FIELDS = {"action", "qty", "confidence", "reason"}


def _validate_decision_shape(d: dict[str, Any]) -> None:
    """Reject decisions that the receiver clearly didn't follow the schema for.

    Real semantic validation (qty bounds, action in BUY/SELL/HOLD, etc.)
    happens later via `enforce_constraints` from strategy_llm.py — we
    deliberately don't repeat that here so policy lives in one place.
    """
    if not isinstance(d, dict):
        raise ValueError(f"decision must be a JSON object, got {type(d).__name__}")
    missing = _REQUIRED_DECISION_FIELDS - d.keys()
    if missing:
        raise ValueError(
            f"decision missing required fields: {sorted(missing)}; "
            f"got {sorted(d)}"
        )
