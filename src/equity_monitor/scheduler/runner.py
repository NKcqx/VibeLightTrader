from __future__ import annotations

import logging
import signal
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import structlog
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from equity_monitor.config import AppConfig, WatchlistConfig
from equity_monitor.db import init_schema, make_engine, make_sessionmaker
from equity_monitor.futu_client import FutuClient, OpenDClient
from equity_monitor.scheduler.calendar import is_trading_day
from equity_monitor.scheduler.jobs import (
    _make_default_sender,
    run_closing_brief,
    run_intraday_check,
    run_morning_brief,
    run_news_pulse,
)


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

    def with_client(
        job_fn: Callable[..., Any], *, kind: str | None = None
    ) -> Callable[[], Any]:
        def runner() -> Any:
            client = client_factory()
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
                return job_fn(**kw)
            finally:
                try:
                    client.close()
                except Exception:
                    pass

        runner.__name__ = job_fn.__name__
        return runner

    def news_runner() -> Any:
        # sentiment_history=None → use DB-backed sentiment_snapshots table.
        return run_news_pulse(
            factory=factory,
            cfg=cfg,
            watchlist=watchlist,
            send_card_fn=sender,
        )

    news_runner.__name__ = "run_news_pulse"

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
    sched.add_job(
        _wrap_trading_day(news_runner, tz_name=tz),
        CronTrigger.from_crontab(
            cfg.scheduler.jobs["news_pulse"].cron, timezone=tz
        ),
        id="news_pulse",
        misfire_grace_time=300,
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
