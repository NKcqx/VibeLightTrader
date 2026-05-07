from __future__ import annotations

import logging
import signal
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from vibe_trader.config import AppConfig, WatchlistConfig
from vibe_trader.db import init_schema, make_engine, make_sessionmaker
from vibe_trader.futu_client import FutuClient, OpenDClient
from vibe_trader.scheduler.calendar import is_trading_day
from vibe_trader.scheduler.jobs import (
    _make_default_image_sender,
    _make_default_sender,
    run_closing_brief,
    run_intraday_check,
    run_morning_brief,
)
from vibe_trader.trader.paper import OpenDSecTrader


def _setup_logging(level: str = "INFO", file_path: str | None = None) -> None:
    logging.basicConfig(level=level)
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ]
    )


def _wrap_trading_day(
    fn: Callable[..., Any], *, tz_name: str
) -> Callable[..., Any]:
    """Skip the wrapped job on US non-trading days.

    Trading-day check is done in `America/New_York` regardless of host TZ.
    Caller passes the configured scheduler tz (typically America/New_York).
    """
    log = structlog.get_logger("scheduler.runner")

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            from zoneinfo import ZoneInfo

            ny_today = datetime.now(tz=timezone.utc).astimezone(ZoneInfo(tz_name)).date()
        except Exception:
            ny_today = datetime.now(tz=timezone.utc).date()
        if not is_trading_day(ny_today):
            log.info("skip.non_trading_day", date=str(ny_today), job=fn.__name__)
            return None
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            log.error("job.failed", job=fn.__name__, error=str(e))

    wrapper.__name__ = fn.__name__
    return wrapper


def build_scheduler(
    *,
    cfg: AppConfig,
    watchlist: WatchlistConfig,
    client_factory: Callable[[], FutuClient] | None = None,
) -> BlockingScheduler:
    sched = BlockingScheduler(timezone=cfg.scheduler.timezone)

    engine = make_engine(cfg.database.path, wal_mode=cfg.database.wal_mode)
    init_schema(engine)
    factory = make_sessionmaker(engine)

    client_factory = client_factory or (
        lambda: OpenDClient(cfg.opend.host, cfg.opend.port)
    )

    sender = _make_default_sender(
        cli_path=cfg.lark.cli_path, identity=cfg.lark.identity
    )
    image_sender = _make_default_image_sender(
        cli_path=cfg.lark.cli_path, identity=cfg.lark.identity
    )

    def _make_paper_trader() -> Any | None:
        """Build the auto-trade broker, or None if disabled in cfg."""
        if not cfg.trader.auto_execute:
            return None
        log_local = structlog.get_logger("scheduler.runner")
        try:
            return OpenDSecTrader(host=cfg.opend.host, port=cfg.opend.port)
        except Exception:
            log_local.exception(
                "scheduler.paper_trader_init_failed_auto_trade_disabled"
            )
            return None

    def with_client(
        job_fn: Callable[..., Any], *, kind: str | None = None
    ) -> Callable[[], Any]:
        def runner() -> Any:
            client = client_factory()
            paper_trader = (
                _make_paper_trader()
                if job_fn.__name__ == "run_intraday_check"
                else None
            )
            try:
                kw: dict[str, Any] = dict(
                    client=client,
                    factory=factory,
                    cfg=cfg,
                    watchlist=watchlist,
                    send_card_fn=sender,
                )
                if kind:
                    kw["kind"] = kind
                if job_fn.__name__ == "run_intraday_check":
                    kw["send_image_fn"] = image_sender
                    kw["snapshot_dir"] = Path("var/snapshots").resolve()
                    kw["paper_trader"] = paper_trader
                return job_fn(**kw)
            finally:
                try:
                    client.close()
                except Exception:
                    pass
                if paper_trader is not None:
                    try:
                        paper_trader.close()
                    except Exception:
                        pass

        runner.__name__ = job_fn.__name__
        return runner

    tz = cfg.scheduler.timezone
    sched.add_job(
        _wrap_trading_day(with_client(run_intraday_check), tz_name=tz),
        CronTrigger.from_crontab(
            cfg.scheduler.jobs["intraday_check"].cron, timezone=tz
        ),
        id="intraday_check",
        misfire_grace_time=300,
    )
    sched.add_job(
        _wrap_trading_day(with_client(run_morning_brief), tz_name=tz),
        CronTrigger.from_crontab(
            cfg.scheduler.jobs["morning_brief"].cron, timezone=tz
        ),
        id="morning_brief",
        misfire_grace_time=600,
    )
    sched.add_job(
        _wrap_trading_day(with_client(run_closing_brief), tz_name=tz),
        CronTrigger.from_crontab(
            cfg.scheduler.jobs["closing_brief"].cron, timezone=tz
        ),
        id="closing_brief",
        misfire_grace_time=600,
    )
    return sched


def run_forever(cfg: AppConfig, watchlist: WatchlistConfig) -> None:
    """Blocking entrypoint: configure logging, build scheduler, install signal handlers, start."""
    _setup_logging(cfg.logging.level, cfg.logging.file)
    sched = build_scheduler(cfg=cfg, watchlist=watchlist)

    def _shutdown(signum: int, frame: Any) -> None:  # noqa: ARG001
        sched.shutdown(wait=False)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    sched.start()
