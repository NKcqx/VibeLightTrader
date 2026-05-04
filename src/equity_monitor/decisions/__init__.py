"""Human-in-the-loop decision pipeline (HITL).

The user has subscriptions to Cursor / Claude / Codex but no programmatic
LLM API key. This module exists to make those subscriptions usable as
the strategy's "LLM" without paying twice — by rendering a self-contained
decision packet that the user pastes into Cursor/Claude, where another
instance of the same model (Claude) runs in their IDE.

Trick: because the SENDER and RECEIVER of the packet are the same model
family, the prompt can do something no external-LLM prompt could: it can
*command* the receiver to use its tools (Read/Grep/Shell) to recall
context from the conversation transcripts, README, and prior audit logs
BEFORE deciding. That is, the prompt isn't just data — it's a
self-instruction script.

Lifecycle:

    [event triggers]
         │
         ▼
    HITLStrategy.decide()
         │  (writes packet, pushes Lark, returns None — no auto-execute)
         ▼
    var/decisions/pending/<id>.md      ◄─ the prompt
    var/decisions/pending/<id>.json    ◄─ raw ctx snapshot for audit
         │
         │  (user opens packet in Cursor → Claude reads MEMORY + decides
         │   → user submits via CLI)
         ▼
    equity-monitor decide submit <id> --json '{"action":..., ...}'
         │
         ▼
    var/decisions/executed/<id>.json   ◄─ decision + trade outcome
         │
         ▼
    enforce_constraints (reused from LLMStrategy)
         │
         ▼
    execute_signal_trade (reused from trader/execute.py)
"""

from equity_monitor.decisions.packet import (
    DecisionPacket,
    render_packet_md,
)
from equity_monitor.decisions.store import (
    PacketState,
    PacketStore,
)

__all__ = [
    "DecisionPacket",
    "render_packet_md",
    "PacketStore",
    "PacketState",
]
