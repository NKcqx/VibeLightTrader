"""Apply parsed Commands to the SQLite Symbols table.

Returns a human-readable reply text for the user. Pure transactional —
errors raise so the listener can format an error reply.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import sessionmaker

from equity_monitor.db import session_scope
from equity_monitor.events.grammar import (
    AddCommand,
    Command,
    HelpCommand,
    ListCommand,
    RemoveCommand,
    ThresholdCommand,
)
from equity_monitor.models import Symbol


HELP_TEXT = (
    "🛠 *equity-monitor 控制指令*\n"
    "\n"
    "**添加监控:**\n"
    "  • `添加 US.AAPL 上限200 下限165`\n"
    "  • `/add US.AAPL upper=200 lower=165`\n"
    "  • `监控 AAPL` (无阈值)\n"
    "\n"
    "**修改阈值:**\n"
    "  • `阈值 US.AAPL 上限205`\n"
    "  • `/threshold US.AAPL upper=205 lower=170`\n"
    "\n"
    "**删除监控:**\n"
    "  • `删除 US.AAPL` / `取消 AAPL`\n"
    "  • `/remove US.AAPL`\n"
    "\n"
    "**查看列表:** `列表` / `/list`\n"
    "**帮助:** `帮助` / `?`"
)


def apply(cmd: Command, factory: sessionmaker) -> str:
    """Dispatch on command type, return reply text."""
    if isinstance(cmd, HelpCommand):
        return HELP_TEXT
    if isinstance(cmd, ListCommand):
        return _do_list(factory)
    if isinstance(cmd, AddCommand):
        return _do_add(cmd, factory)
    if isinstance(cmd, RemoveCommand):
        return _do_remove(cmd, factory)
    if isinstance(cmd, ThresholdCommand):
        return _do_threshold(cmd, factory)
    raise TypeError(f"unknown command type: {type(cmd).__name__}")


def _do_list(factory: sessionmaker) -> str:
    with session_scope(factory) as s:
        rows = s.query(Symbol).order_by(Symbol.code).all()
        if not rows:
            return "📭 监控列表为空。\n用 `添加 US.AAPL 上限200 下限165` 增加标的。"
        lines = ["📋 **当前监控列表:**", ""]
        for r in rows:
            up = f"{r.upper_threshold:.2f}" if r.upper_threshold is not None else "—"
            lo = f"{r.lower_threshold:.2f}" if r.lower_threshold is not None else "—"
            name = f" ({r.name})" if r.name else ""
            lines.append(f"• `{r.code}`{name}  上限 {up} / 下限 {lo}")
        return "\n".join(lines)


def _do_add(cmd: AddCommand, factory: sessionmaker) -> str:
    with session_scope(factory) as s:
        existing = s.query(Symbol).filter(Symbol.code == cmd.code).one_or_none()
        if existing is not None:
            # Treat re-add as an update to thresholds/name.
            changes: list[str] = []
            if cmd.upper is not None and existing.upper_threshold != cmd.upper:
                existing.upper_threshold = cmd.upper
                changes.append(f"上限→{cmd.upper}")
            if cmd.lower is not None and existing.lower_threshold != cmd.lower:
                existing.lower_threshold = cmd.lower
                changes.append(f"下限→{cmd.lower}")
            if cmd.name and existing.name != cmd.name:
                existing.name = cmd.name
                changes.append(f"名字→{cmd.name}")
            if not changes:
                return f"ℹ️ `{cmd.code}` 已在监控中（参数无变化）。"
            return f"♻️ 已更新 `{cmd.code}`: " + ", ".join(changes)

        s.add(
            Symbol(
                code=cmd.code,
                name=cmd.name or cmd.code.split(".")[-1],
                upper_threshold=cmd.upper,
                lower_threshold=cmd.lower,
            )
        )
        bits: list[str] = []
        if cmd.upper is not None:
            bits.append(f"上限 {cmd.upper}")
        if cmd.lower is not None:
            bits.append(f"下限 {cmd.lower}")
        suffix = " (" + ", ".join(bits) + ")" if bits else ""
        return f"✅ 已添加 `{cmd.code}` 到监控列表{suffix}。"


def _do_remove(cmd: RemoveCommand, factory: sessionmaker) -> str:
    with session_scope(factory) as s:
        existing = s.query(Symbol).filter(Symbol.code == cmd.code).one_or_none()
        if existing is None:
            return f"❓ `{cmd.code}` 不在监控列表中。"
        s.delete(existing)
        return f"🗑️ 已从监控列表移除 `{cmd.code}`。"


def _do_threshold(cmd: ThresholdCommand, factory: sessionmaker) -> str:
    with session_scope(factory) as s:
        existing = s.query(Symbol).filter(Symbol.code == cmd.code).one_or_none()
        if existing is None:
            return (
                f"❓ `{cmd.code}` 不在监控列表中。"
                f"先用 `添加 {cmd.code} 上限... 下限...` 添加。"
            )
        changes: list[str] = []
        if cmd.upper is not None:
            existing.upper_threshold = cmd.upper
            changes.append(f"上限→{cmd.upper}")
        if cmd.lower is not None:
            existing.lower_threshold = cmd.lower
            changes.append(f"下限→{cmd.lower}")
        if not changes:
            return f"ℹ️ `{cmd.code}` 阈值未变更。"
        return f"♻️ `{cmd.code}` 阈值更新: " + ", ".join(changes)
