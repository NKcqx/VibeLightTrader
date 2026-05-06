"""Human-in-the-Loop strategy.

Same Strategy Protocol as RuleStrategy / LLMStrategy, but instead of
deciding inline it:

  1. builds a `DecisionPacket` from the current StrategyContext,
  2. writes it to the on-disk packet store as PENDING,
  3. (best-effort) pushes a Lark notification with the packet path,
  4. returns None — meaning "no programmatic suggestion this tick."

Execution happens later, asynchronously, when the user runs
`vibe-trader decide submit <id> --json '...'`.

Why a strategy and not a separate scheduler-level hook? Because every
other strategy fits the same Protocol — making HITL a Strategy means we
get the existing wiring for free: cfg.trader.strategy.type → registry
→ build_strategy(...). Switching to HITL is a one-line yaml flip.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import structlog

from vibe_trader.decisions.packet import (
    build_packet,
    default_memory_hints,
)
from vibe_trader.decisions.store import PacketStore
from vibe_trader.signals.strategy_base import (
    Strategy,
    StrategyContext,
    register_strategy,
)
from vibe_trader.signals.strategy_lite import SignalSuggest

log = structlog.get_logger(__name__)


# Type alias: anything that takes a markdown body and returns a Lark
# message id. Keeps the strategy decoupled from the actual lark-cli
# wrapper so unit tests can inject a fake.
LarkPushFn = Callable[[str], str]


@dataclass
class HITLStrategy:
    """HITL strategy. Always returns None from `decide`.

    Construction:
        store: a PacketStore pointing at var/decisions/
        repo_root: passed into render_packet_md to make the submit
            command and write-path absolute (some users open Cursor
            from a different CWD).
        constraints: dict mirroring StrategyLLMConfig knobs so the
            packet displays the same hard limits the executor will use.
        lark_push: optional callable(md_body) -> message_id; if None,
            packet is created but no notification is pushed (still
            visible via `vibe-trader decide list`).
        ttl_signal_ids: how many of the latest persisted signal ids to
            attach to the packet (drives idempotency on submit). The
            scheduler caches them in `triggering_signal_ids`.
    """

    store: PacketStore
    repo_root: Path | None = None

    name: str = "hitl"

    max_position: int = 200
    min_trade_size: int = 10
    min_confidence: float = 0.6

    lark_push: LarkPushFn | None = None
    """If set, called once per packet with the rendered markdown body."""

    extra_memory_hints: list[str] = field(default_factory=list)
    """Extra `Read`/`Grep` commands appended after the defaults."""

    def decide(self, ctx: StrategyContext) -> SignalSuggest | None:
        """Produce a packet, push to Lark, then return None.

        Returns None unconditionally — HITL never auto-trades. None ALSO
        prevents `_persist_signal_rows` from attaching a suggested_action
        to the SignalRow, which is correct: nothing has been suggested
        until the user/Claude submits via CLI.
        """
        if not ctx.signals:
            return None

        # Caller (jobs.py) doesn't know about persisted SignalRow ids
        # at decide() time; we attach them post-hoc from
        # triggering_signal_types. The CLI submit step will resolve to
        # the actual rowid via this list, so empty is fine.
        constraints = {
            "max_position": self.max_position,
            "min_trade_size": self.min_trade_size,
            "min_confidence": self.min_confidence,
        }
        hints = default_memory_hints(self.repo_root, code=ctx.code) + list(
            self.extra_memory_hints
        )
        packet = build_packet(
            ctx,
            triggering_signal_ids=[],  # populated post-persist; see jobs.py
            constraints=constraints,
            memory_hints=hints,
        )
        stored = self.store.write_pending(packet, repo_root=self.repo_root)
        log.info(
            "hitl.packet_pending",
            id=packet.id,
            code=ctx.code,
            triggers=[s.signal_type for s in ctx.signals],
            md_path=str(stored.md_path),
        )

        if self.lark_push is not None:
            try:
                # Don't push the entire 100+ line packet to Lark — push a
                # compact summary with the file path + submit command.
                summary = self._build_lark_summary(stored.md_path, packet)
                msg_id = self.lark_push(summary)
                log.info("hitl.lark_pushed", id=packet.id, msg_id=msg_id)
            except Exception as e:
                log.warning(
                    "hitl.lark_push_failed",
                    id=packet.id,
                    exc_type=type(e).__name__,
                    error=repr(e),
                )

        return None  # explicitly: no programmatic suggestion this tick

    def _build_lark_summary(
        self, md_path: Path, packet: Any, *, repo_root: Path | None = None
    ) -> str:
        """One-screen summary the user actually skims on phone.

        Layout invariant: every block of text the user is supposed to
        COPY is wrapped in a fenced ``` block of its own. No prose
        instructions are mixed inside a copyable block. The user
        long-presses → "select code block" → paste; nothing to read,
        nothing to trim.

        Two copyable blocks:

          - Block A: prompt for the Cursor-side Claude. The user
            pastes this into Cursor's chat. Claude reads the packet,
            decides, and (if it has shell access) submits via CLI.

          - Block B: the literal CLI command. Used when Claude can't
            run shell, or when the user wants to type the JSON
            themselves.
        """
        triggers = ", ".join(packet.triggering_signal_types) or "(无信号)"
        price = "n/a"
        if packet.snapshot:
            v = packet.snapshot.get("last_price")
            if v is not None:
                try:
                    price = f"${float(v):.2f}"
                except (TypeError, ValueError):
                    price = str(v)
        intraday = ""
        if packet.intraday_return is not None:
            sign = "▲" if packet.intraday_return >= 0 else "▼"
            intraday = f" · 日内 {sign} {packet.intraday_return:+.2%}"

        repo = str(repo_root or self.repo_root or Path.cwd())
        # md_path is whatever PacketStore wrote — usually relative when
        # the scheduler ran from the repo root. The Cursor-side Claude
        # may have a different cwd, so we resolve to absolute.
        abs_md_path = str(Path(md_path).resolve())

        # Block A: the Cursor prompt. Self-contained — gives Claude the
        # packet path, the repo root, the submit command template.
        # Use a `text` fence (not bash) so flat-rendering clients don't
        # syntax-highlight the prose.
        cursor_prompt = (
            f"请帮我处理 vibe-trader 决策 packet `{packet.id}`：\n"
            f"1. 用 Read 工具读 `{abs_md_path}` 看完整 prompt；\n"
            f"2. 按 packet 里第 1-4 步的 self-instructions 做决策（含 grep MEMORY）；\n"
            f"3. 用 Shell 工具在 `{repo}` 目录下执行：\n"
            f"   vibe-trader decide submit {packet.id} --json '<你的决策 JSON>'"
        )

        # Block B: the literal command. Single line so mobile copy
        # doesn't break it into pieces.
        submit_cmd = (
            f"cd {repo} && vibe-trader decide submit {packet.id} "
            f"--json '<paste Claude 的 JSON 这里>'"
        )

        return (
            f"🎯 **HITL 决策待办** · `{packet.code}`\n\n"
            f"**Packet ID**: `{packet.id}`\n"
            f"**触发信号**: {triggers}\n"
            f"**当前价**: {price}{intraday}\n"
            f"**持仓**: {packet.position_qty} 股 @ ${packet.avg_cost:.2f}\n\n"
            f"---\n"
            f"**操作 A — 自动模式（推荐）**：复制下面整段 → 粘到 Cursor 对话框 → 让 Claude 自己处理\n\n"
            f"```text\n"
            f"{cursor_prompt}\n"
            f"```\n\n"
            f"---\n"
            f"**操作 B — 手动模式**：把 packet 文件内容贴给任何 LLM 拿到 JSON 后，复制下面这条命令到终端，"
            f"把 `<paste Claude 的 JSON 这里>` 替换为 JSON 字符串（注意保留外层单引号）\n\n"
            f"```bash\n"
            f"{submit_cmd}\n"
            f"```"
        )


# ---------------------------------------------------------------------------
# Registry hook. NOTE: HITLStrategy needs a PacketStore at construction,
# but our registry signature is `build_strategy(name, config: dict)`.
# So we accept `var_dir` in the config and lazy-init the store here.
# ---------------------------------------------------------------------------


def _build_hitl_strategy(config: dict[str, Any]) -> Strategy:
    cfg = dict(config)
    var_dir = Path(cfg.pop("var_dir", "var/decisions"))
    repo_root_raw = cfg.pop("repo_root", None)
    repo_root = Path(repo_root_raw) if repo_root_raw else None

    store = PacketStore(var_dir)

    # lark_push and extra_memory_hints can't be set via yaml (they're
    # callable / runtime); jobs.py wires them in via attribute injection
    # right after build_strategy(). For tests, callers can construct
    # HITLStrategy directly.
    return HITLStrategy(
        store=store,
        repo_root=repo_root,
        max_position=cfg.pop("max_position_per_symbol", 200),
        min_trade_size=cfg.pop("min_trade_size", 10),
        min_confidence=cfg.pop("min_confidence", 0.6),
    )


try:
    register_strategy("hitl")(_build_hitl_strategy)
except ValueError:
    # already registered (test reload); fine.
    pass
