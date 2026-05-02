from __future__ import annotations

from pathlib import Path
from typing import Any

import click

from equity_monitor.config import load_settings, load_watchlist
from equity_monitor.data.backfill import backfill_all
from equity_monitor.db import init_schema, make_engine, make_sessionmaker, session_scope
from equity_monitor.futu_client import OpenDClient
from equity_monitor.models import Position, Symbol
from equity_monitor.models import Signal as SignalRow
from equity_monitor.models import Trade
from equity_monitor.scheduler.jobs import (
    run_closing_brief,
    run_intraday_check,
    run_morning_brief,
    run_news_pulse,
)
from equity_monitor.scheduler.runner import run_forever


@click.group()
@click.option(
    "--settings",
    "settings_path",
    default="config/settings.yaml",
    show_default=True,
    type=click.Path(),
)
@click.option(
    "--watchlist",
    "watchlist_path",
    default="config/watchlist.yaml",
    show_default=True,
    type=click.Path(),
)
@click.pass_context
def cli(ctx: click.Context, settings_path: str, watchlist_path: str) -> None:
    """Equity Monitor — hourly US-equity monitor with Lark alerts."""
    ctx.ensure_object(dict)
    # Lazy: store paths only; load configs on first access in subcommand
    # so `--help` works even if config files don't exist yet.
    ctx.obj["settings_path"] = settings_path
    ctx.obj["watchlist_path"] = watchlist_path


def _get_cfg(ctx: click.Context) -> Any:
    if "cfg" not in ctx.obj:
        ctx.obj["cfg"] = load_settings(ctx.obj["settings_path"])
    return ctx.obj["cfg"]


def _get_watchlist(ctx: click.Context) -> Any:
    if "watchlist" not in ctx.obj:
        ctx.obj["watchlist"] = load_watchlist(ctx.obj["watchlist_path"])
    return ctx.obj["watchlist"]


def _make_factory(cfg: Any) -> Any:
    Path(cfg.database.path).parent.mkdir(parents=True, exist_ok=True)
    engine = make_engine(cfg.database.path, wal_mode=cfg.database.wal_mode)
    init_schema(engine)
    return make_sessionmaker(engine)


@cli.command()
@click.pass_context
def run(ctx: click.Context) -> None:
    """Start the long-running scheduler (blocking; SIGINT/SIGTERM to stop)."""
    run_forever(_get_cfg(ctx), _get_watchlist(ctx))


@cli.command()
@click.pass_context
def listen(ctx: click.Context) -> None:
    """Start the Lark message listener (blocking; SIGINT to stop).

    Subscribes to im.message.receive_v1 events as the configured bot and
    dispatches recognized commands (/add, /remove, /list, /threshold, /help).
    Pair with `equity-monitor run` in another tmux pane.
    """
    from equity_monitor.events.listener import run_listener

    cfg = _get_cfg(ctx)
    factory = _make_factory(cfg)
    click.echo("listener starting (Ctrl-C to stop)…")
    run_listener(cfg=cfg, factory=factory)


@cli.command()
@click.option(
    "--job",
    type=click.Choice(["intraday", "morning", "closing", "news"]),
    required=True,
    help="Which single job to run.",
)
@click.pass_context
def once(ctx: click.Context, job: str) -> None:
    """Run a single job once and print the result dict."""
    cfg = _get_cfg(ctx)
    wl = _get_watchlist(ctx)
    factory = _make_factory(cfg)

    if job == "news":
        res = run_news_pulse(factory=factory, cfg=cfg, watchlist=wl)
    else:
        client = OpenDClient(cfg.opend.host, cfg.opend.port)
        try:
            if job == "intraday":
                res = run_intraday_check(
                    client=client, factory=factory, cfg=cfg, watchlist=wl
                )
            elif job == "morning":
                res = run_morning_brief(
                    client=client, factory=factory, cfg=cfg, watchlist=wl
                )
            else:
                res = run_closing_brief(
                    client=client, factory=factory, cfg=cfg, watchlist=wl
                )
        finally:
            client.close()
    click.echo(res)


@cli.group()
def watchlist() -> None:
    """Watchlist subcommands."""


@watchlist.command("list")
@click.pass_context
def watchlist_list(ctx: click.Context) -> None:
    """List active symbols in the database."""
    cfg = _get_cfg(ctx)
    factory = _make_factory(cfg)
    with session_scope(factory) as s:
        rows = (
            s.query(Symbol)
            .filter(Symbol.is_active.is_(True))
            .order_by(Symbol.code)
            .all()
        )
        if not rows:
            click.echo("(no active symbols — try `watchlist sync`)")
            return
        for sym in rows:
            click.echo(
                f"{sym.code:12s}  upper={sym.upper_threshold}  "
                f"lower={sym.lower_threshold}  name={sym.name}"
            )


@watchlist.command("sync")
@click.pass_context
def watchlist_sync(ctx: click.Context) -> None:
    """Sync watchlist.yaml → symbols table (idempotent upsert).

    Existing rows have their thresholds/notes overwritten and are reactivated.
    """
    cfg = _get_cfg(ctx)
    wl = _get_watchlist(ctx)
    factory = _make_factory(cfg)

    with session_scope(factory) as s:
        for sc in wl.symbols:
            sym = s.query(Symbol).filter(Symbol.code == sc.code).one_or_none()
            if sym is None:
                s.add(
                    Symbol(
                        code=sc.code,
                        name=sc.name,
                        upper_threshold=sc.upper_threshold,
                        lower_threshold=sc.lower_threshold,
                        notes=sc.notes,
                        is_active=True,
                    )
                )
            else:
                sym.name = sc.name
                sym.upper_threshold = sc.upper_threshold
                sym.lower_threshold = sc.lower_threshold
                sym.notes = sc.notes
                sym.is_active = True
    click.echo(f"synced {len(wl.symbols)} symbols")


@cli.group()
def db() -> None:
    """DB subcommands."""


@db.command("init")
@click.pass_context
def db_init(ctx: click.Context) -> None:
    """Initialize SQLite schema (creates DB file if missing)."""
    cfg = _get_cfg(ctx)
    _make_factory(cfg)
    click.echo(f"initialized {cfg.database.path}")


@db.command("status")
@click.pass_context
def db_status(ctx: click.Context) -> None:
    """Print row counts of the main tables."""
    cfg = _get_cfg(ctx)
    factory = _make_factory(cfg)
    from equity_monitor.models import (
        Indicator,
        NewsDigest,
        Quote,
        SentimentSnapshotRow,
    )
    from equity_monitor.models import Signal as SignalRow

    with session_scope(factory) as s:
        click.echo(f"db.path:           {cfg.database.path}")
        click.echo(f"symbols:           {s.query(Symbol).count()}")
        click.echo(f"  active:          {s.query(Symbol).filter(Symbol.is_active.is_(True)).count()}")
        click.echo(f"quotes:            {s.query(Quote).count()}")
        click.echo(f"indicators:        {s.query(Indicator).count()}")
        click.echo(f"signals:           {s.query(SignalRow).count()}")
        click.echo(f"news_digest:       {s.query(NewsDigest).count()}")
        click.echo(f"sentiment_snapshots: {s.query(SentimentSnapshotRow).count()}")


# ---------------------------------------------------------------------------
# Phase 2: paper trading CLI
# ---------------------------------------------------------------------------


def _make_trader(cfg: Any) -> Any:
    """Create the real OpenD-backed paper trader. Tests patch this."""
    from equity_monitor.trader.paper import OpenDSecTrader

    return OpenDSecTrader(host=cfg.opend.host, port=cfg.opend.port)


@cli.group()
def trade() -> None:
    """Phase 2 paper-trading subcommands."""


@trade.command("list")
@click.option(
    "--status",
    type=click.Choice(["pending", "confirmed", "executed", "cancelled", "all"]),
    default="pending",
    show_default=True,
)
@click.pass_context
def trade_list(ctx: click.Context, status: str) -> None:
    """Show today's signal suggestions with their signal_id."""
    cfg = _get_cfg(ctx)
    factory = _make_factory(cfg)
    with session_scope(factory) as s:
        q = s.query(SignalRow).filter(SignalRow.suggested_action.isnot(None))
        if status != "all":
            q = q.filter(SignalRow.status == status)
        rows = q.order_by(SignalRow.ts.desc()).limit(50).all()
        if not rows:
            click.echo(f"(no signals with status={status})")
            return
        sym_by_id = {sym.id: sym for sym in s.query(Symbol).all()}
        click.echo(f"{'id':>5}  {'code':12s}  {'action':6s}  {'qty':>4}  status     ts")
        for sig in rows:
            code = sym_by_id[sig.symbol_id].code
            click.echo(
                f"{sig.id:>5}  {code:12s}  {sig.suggested_action or '-':6s}  "
                f"{sig.suggested_qty or 0:>4}  {sig.status:10s} {sig.ts.isoformat(timespec='minutes')}"
            )


def _execute_paper_trade(
    s: Any, sig: SignalRow, sym: Symbol, qty: int, trader: Any
) -> int:
    """Place order via trader, persist Trade row, update Position, mutate sig.

    Returns the new trade.id. Raises on rejection.
    """
    side = sig.suggested_action
    if side not in ("BUY", "SELL"):
        raise click.ClickException(
            f"signal {sig.id} suggested_action={side!r} is not actionable"
        )

    result = trader.place_order(code=sym.code, side=side, qty=qty)
    if result.status == "REJECTED":
        sig.status = "cancelled"
        raise click.ClickException(
            f"order rejected by paper broker: {result.error}"
        )

    trade_row = Trade(
        symbol_id=sym.id,
        ts=result.submitted_at,
        side=side,
        qty=result.filled_qty,
        price=result.avg_fill_price,
        futu_order_id=result.order_id,
        signal_id=sig.id,
        status=result.status,
    )
    s.add(trade_row)
    s.flush()  # populate trade_row.id

    pos = s.query(Position).filter(Position.symbol_id == sym.id).one_or_none()
    if side == "BUY":
        if pos is None:
            s.add(
                Position(
                    symbol_id=sym.id,
                    qty=qty,
                    avg_cost=result.avg_fill_price,
                )
            )
        else:
            new_qty = pos.qty + qty
            pos.avg_cost = (
                (pos.qty * pos.avg_cost) + (qty * result.avg_fill_price)
            ) / new_qty
            pos.qty = new_qty
    else:  # SELL
        assert pos is not None and pos.qty >= qty, "oversold past broker check?"
        realized = (result.avg_fill_price - pos.avg_cost) * qty
        pos.realized_pnl = (pos.realized_pnl or 0.0) + realized
        pos.qty -= qty
        if pos.qty == 0:
            pos.avg_cost = 0.0

    sig.status = "executed"
    sig.executed_trade_id = trade_row.id
    return trade_row.id


@trade.command("confirm")
@click.argument("signal_id", type=int)
@click.option(
    "--qty",
    type=int,
    default=None,
    help="Override the suggested qty (default: use the suggestion).",
)
@click.pass_context
def trade_confirm(ctx: click.Context, signal_id: int, qty: int | None) -> None:
    """Place a paper order for a pending suggestion."""
    cfg = _get_cfg(ctx)
    factory = _make_factory(cfg)
    trader = _make_trader(cfg)

    try:
        with session_scope(factory) as s:
            sig = s.query(SignalRow).filter(SignalRow.id == signal_id).one_or_none()
            if sig is None:
                raise click.ClickException(f"signal {signal_id} not found")
            if sig.status == "executed":
                click.echo(
                    f"signal {signal_id} already executed (trade_id={sig.executed_trade_id})"
                )
                return
            if sig.status != "pending":
                raise click.ClickException(
                    f"signal {signal_id} status={sig.status}, can only confirm pending"
                )
            if sig.suggested_action is None:
                raise click.ClickException(
                    f"signal {signal_id} has no suggested_action"
                )
            sym = s.query(Symbol).filter(Symbol.id == sig.symbol_id).one()
            chosen_qty = qty if qty is not None else (sig.suggested_qty or 0)
            if chosen_qty <= 0:
                raise click.ClickException(
                    f"qty must be positive (got {chosen_qty})"
                )
            trade_id = _execute_paper_trade(s, sig, sym, chosen_qty, trader)
            click.echo(
                f"placed paper order: signal_id={signal_id} trade_id={trade_id} "
                f"{sig.suggested_action} {chosen_qty} {sym.code}"
            )
    finally:
        try:
            trader.close()
        except Exception:
            pass


@trade.command("cancel")
@click.argument("signal_id", type=int)
@click.pass_context
def trade_cancel(ctx: click.Context, signal_id: int) -> None:
    """Mark a pending suggestion as cancelled (no order is placed)."""
    cfg = _get_cfg(ctx)
    factory = _make_factory(cfg)
    with session_scope(factory) as s:
        sig = s.query(SignalRow).filter(SignalRow.id == signal_id).one_or_none()
        if sig is None:
            raise click.ClickException(f"signal {signal_id} not found")
        if sig.status != "pending":
            raise click.ClickException(
                f"signal {signal_id} status={sig.status}, only pending can be cancelled"
            )
        sig.status = "cancelled"
    click.echo(f"cancelled signal {signal_id}")


@trade.command("positions")
@click.pass_context
def trade_positions(ctx: click.Context) -> None:
    """Show open paper positions with mark-to-market P&L (DB-side)."""
    cfg = _get_cfg(ctx)
    factory = _make_factory(cfg)
    with session_scope(factory) as s:
        rows = s.query(Position).filter(Position.qty > 0).all()
        if not rows:
            click.echo("(no open positions)")
            return
        sym_by_id = {sym.id: sym for sym in s.query(Symbol).all()}
        click.echo(f"{'code':12s}  {'qty':>5}  {'avg_cost':>10}  {'realized_pnl':>14}")
        for p in rows:
            code = sym_by_id[p.symbol_id].code
            click.echo(
                f"{code:12s}  {p.qty:>5}  {p.avg_cost:>10.2f}  "
                f"{p.realized_pnl:>14.2f}"
            )


@trade.command("pnl")
@click.option("--days", type=int, default=7, show_default=True)
@click.pass_context
def trade_pnl(ctx: click.Context, days: int) -> None:
    """Print cumulative realized P&L by symbol for the last N days."""
    from datetime import datetime, timedelta, timezone

    cfg = _get_cfg(ctx)
    factory = _make_factory(cfg)
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)
    with session_scope(factory) as s:
        trades = s.query(Trade).filter(Trade.ts >= cutoff).all()
        if not trades:
            click.echo(f"(no trades in the last {days} days)")
            return
        sym_by_id = {sym.id: sym for sym in s.query(Symbol).all()}
        # Recompute realized P&L by replaying fills (FIFO via avg-cost)
        ledger: dict[str, dict[str, float]] = {}
        for t in sorted(trades, key=lambda x: x.ts):
            code = sym_by_id[t.symbol_id].code
            book = ledger.setdefault(code, {"qty": 0.0, "avg_cost": 0.0, "realized": 0.0})
            if t.side == "BUY":
                new_qty = book["qty"] + t.qty
                book["avg_cost"] = (
                    book["qty"] * book["avg_cost"] + t.qty * t.price
                ) / new_qty if new_qty > 0 else 0.0
                book["qty"] = new_qty
            else:
                realized = (t.price - book["avg_cost"]) * t.qty
                book["realized"] += realized
                book["qty"] -= t.qty
                if book["qty"] <= 0:
                    book["avg_cost"] = 0.0

        total = 0.0
        click.echo(
            f"{'code':12s}  {'fills':>5}  realized_pnl"
        )
        for code, book in ledger.items():
            n = sum(1 for t in trades if sym_by_id[t.symbol_id].code == code)
            click.echo(f"{code:12s}  {n:>5}  {book['realized']:>+12.2f}")
            total += book["realized"]
        click.echo(f"{'TOTAL':12s}  {'':>5}  {total:>+12.2f}")


@cli.command()
@click.option(
    "--days",
    default=30,
    show_default=True,
    type=int,
    help="How many calendar days of 60-min K-line to pull (~7 bars per US trading day).",
)
@click.pass_context
def backfill(ctx: click.Context, days: int) -> None:
    """Backfill historical 60-min OHLC + indicators for the entire watchlist."""
    cfg = _get_cfg(ctx)
    wl = _get_watchlist(ctx)
    factory = _make_factory(cfg)

    client = OpenDClient(cfg.opend.host, cfg.opend.port)
    try:
        out = backfill_all(
            client=client,
            factory=factory,
            codes=[s.code for s in wl.symbols],
            days=days,
        )
    finally:
        client.close()
    for code, stats in out.items():
        click.echo(
            f"{code}: quotes={stats['quotes']} indicators={stats['indicators']}"
        )


if __name__ == "__main__":
    cli()
