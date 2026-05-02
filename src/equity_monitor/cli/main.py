from __future__ import annotations

from pathlib import Path
from typing import Any

import click

from equity_monitor.config import load_settings, load_watchlist
from equity_monitor.data.backfill import backfill_all
from equity_monitor.db import init_schema, make_engine, make_sessionmaker, session_scope
from equity_monitor.futu_client import OpenDClient
from equity_monitor.models import Symbol
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
