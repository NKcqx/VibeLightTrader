"""Apply parsed Commands to the SQLite Symbols table.

Returns a human-readable reply text for the user. Pure transactional —
errors raise so the listener can format an error reply.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from sqlalchemy.orm import sessionmaker

from vibe_trader.data.kline import fetch_kline_df
from vibe_trader.db import session_scope
from vibe_trader.events.grammar import (
    AddCommand,
    ChartCommand,
    Command,
    HelpCommand,
    ListCommand,
    RemoveCommand,
    ThresholdCommand,
)
from vibe_trader.futu_client import FREQ_TO_KTYPE, FutuClient
from vibe_trader.models import Position, Symbol, Trade
from vibe_trader.reports.snapshot import SnapshotRequest, TradeMarker, render_snapshot


HELP_TEXT = (
    "🛠 **vibe-trader · 飞书指令清单**\n"
    "\n"
    "📋 **查看监控**\n"
    "  • `列表` / `/list` / `ls` — 当前所有标的 + 实时价 + 指标\n"
    "\n"
    "➕ **添加监控**\n"
    "  • `添加 US.AAPL 上限200 下限165`\n"
    "  • `监控 TSLA`  (无阈值，仅追踪)\n"
    "  • `/add US.NVDA upper=180 lower=110`\n"
    "  • 别名：`添加` / `增加` / `监控` / `关注` / `/add`\n"
    "\n"
    "🎯 **修改阈值**\n"
    "  • `阈值 US.AAPL 上限290 下限200`\n"
    "  • `/threshold AAPL upper=290 lower=200`\n"
    "  • 别名：`阈值` / `修改` / `更新` / `/threshold`\n"
    "\n"
    "🗑 **删除监控**\n"
    "  • `删除 US.AAPL` / `取消 AAPL` / `/remove AAPL`\n"
    "  • 别名：`删除` / `取消` / `停止` / `不监控` / `/remove`\n"
    "\n"
    "📈 **K 线快照**\n"
    "  • `/chart US.AAPL` — 60m 默认；显示买卖点 + 成本线 + 现价线\n"
    "  • `/chart US.AAPL D` — 日 K，可选 5m/15m/30m/60m/D/W\n"
    "  • 别名：`/chart` / `chart` / `图`\n"
    "\n"
    "ℹ️ **使用说明**\n"
    "  • 标的代码：`US.AAPL` / `HK.0700`，裸代码 `AAPL` 自动加 `US.` 前缀\n"
    "  • 阈值关键词：`上限/下限`、`阻力位/支撑位`、`upper/lower`\n"
    "  • 三种风格通用：中文自然语言 / 英文关键字 / `/` 命令\n"
    "  • 当前价 ≥ 上限或 ≤ 下限会自动推送 CRITICAL 信号卡"
)


@dataclass(frozen=True)
class ChartReplyPayload:
    image_path: Path


def _avg_cost_from_markers(markers: list[TradeMarker]) -> float | None:
    """Replay BUY/SELL markers chronologically to derive current avg cost.

    BUY adds at the trade price; SELL reduces qty at the *running* avg cost
    (i.e. doesn't change the per-share basis). Returns None if there's no
    open position after replay.

    Markers are expected to already be in chronological order (apply_chart
    queries `ORDER BY ts ASC`); we don't re-sort to keep this trivially
    auditable.
    """
    qty = 0
    cost_basis = 0.0
    for m in markers:
        if m.price <= 0:
            # Skip placeholder rows (MARKET orders submitted but the
            # broker hasn't reported the fill price back to our DB yet —
            # `_reconcile_pending_fills` is responsible for healing these).
            continue
        if m.side == "buy":
            qty += m.qty
            cost_basis += m.qty * m.price
        elif m.side == "sell":
            if qty <= 0:
                # Short or stale data — skip; we don't model shorts here.
                continue
            avg = cost_basis / qty
            sell_qty = min(m.qty, qty)
            cost_basis -= sell_qty * avg
            qty -= sell_qty
    if qty <= 0:
        return None
    return cost_basis / qty


def apply_chart(
    cmd: ChartCommand,
    factory: sessionmaker,
    *,
    client: FutuClient,
    snapshot_dir: Path | None = None,
    now_utc: datetime | None = None,
) -> tuple[str, ChartReplyPayload]:
    """Render a K-line snapshot for `cmd.code` at `cmd.freq`.

    Returns (markdown reply text, payload with PNG path).
    Raises on data fetch / render failure — caller logs & fallbacks.
    """
    if now_utc is None:
        now_utc = datetime.now(tz=timezone.utc)
    elif now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    ktype = FREQ_TO_KTYPE.get(cmd.freq)
    if ktype is None:
        raise ValueError(f"unsupported freq for chart: {cmd.freq!r}")

    df = fetch_kline_df(client, cmd.code, ktype=ktype, limit=200)

    trade_window_start = now_utc - timedelta(days=30)
    markers: list[TradeMarker] = []
    avg_cost: float | None = None
    with session_scope(factory) as session:
        sym = session.query(Symbol).filter(Symbol.code == cmd.code).one_or_none()
        if sym is not None:
            trade_rows = (
                session.query(Trade)
                .filter(
                    Trade.symbol_id == sym.id,
                    Trade.ts >= trade_window_start,
                )
                .order_by(Trade.ts.asc())
                .all()
            )
            for r in trade_rows:
                side_norm = r.side.strip().upper()
                if side_norm == "BUY":
                    side_lit: Literal["buy", "sell"] = "buy"
                elif side_norm == "SELL":
                    side_lit = "sell"
                else:
                    continue
                if r.price <= 0:
                    # Unfilled MARKET order placeholder — rendering would
                    # plant a triangle at $0 on the chart. Drop until
                    # reconcile fills the price in.
                    continue
                ts = r.ts if r.ts.tzinfo else r.ts.replace(tzinfo=timezone.utc)
                markers.append(
                    TradeMarker(ts=ts, side=side_lit, qty=r.qty, price=r.price)
                )
            position = (
                session.query(Position)
                .filter(Position.symbol_id == sym.id, Position.qty > 0)
                .one_or_none()
            )
            if position is not None:
                avg_cost = position.avg_cost
            else:
                # Fallback: replay BUY/SELL trades to derive avg_cost.
                # Why we need this: execute_signal_trade only writes the
                # Position table when the broker returns status=FILLED. Our
                # OpenD paper trades land as PENDING (broker async fill is
                # not polled back yet), so positions stays empty even
                # though there's an actual paper position upstream. Without
                # this fallback the chart silently drops the avg-cost line.
                avg_cost = _avg_cost_from_markers(markers)

    try:
        snap = next(iter(client.snapshot([cmd.code])), None)
    except Exception:
        snap = None
    current_price = float(snap.last_price) if snap is not None else None

    req = SnapshotRequest(
        code=cmd.code,
        freq=cmd.freq,
        df=df,
        markers=markers,
        avg_cost=avg_cost,
        current_price=current_price,
        out_dir=snapshot_dir,
    )
    png = render_snapshot(req)

    bits: list[str] = [f"📈 `{cmd.code}` · {cmd.freq}"]
    if current_price is not None:
        bits.append(f"现价 ${current_price:.2f}")
    if avg_cost is not None:
        bits.append(f"成本 ${avg_cost:.2f}")
    bits.append(f"{len(markers)} 笔近 30 日交易")
    text = " · ".join(bits)
    return text, ChartReplyPayload(image_path=png)


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
    if isinstance(cmd, ChartCommand):
        raise TypeError(
            "ChartCommand must be dispatched via apply_chart, not apply()."
        )
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
