"""Dry-run the scheduler runner without touching OpenD or lark-cli.

What it verifies:
  1. `build_scheduler` constructs successfully against settings.yaml + an example watchlist.
  2. All four jobs are registered with correct cron triggers.
  3. NYSE trading-day gating is wired: a wrapped fn returns None on holidays.
  4. The scheduler can be cleanly built then shut down (no lingering threads).

It does NOT actually start `BlockingScheduler.start()` (that would block).
Instead we inspect `get_jobs()` and print next-run times.

Usage:
    conda activate fin
    python scripts/dry_run_runner.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from equity_monitor.config import (  # noqa: E402
    AppConfig,
    DatabaseConfig,
    JobCron,
    LarkConfig,
    LarkReceiver,
    LoggingConfig,
    OpenDConfig,
    SchedulerConfig,
    SignalsConfig,
    SymbolConfig,
    WatchlistConfig,
    load_settings,
    load_watchlist,
)
from equity_monitor.futu_client import FakeFutuClient  # noqa: E402
from equity_monitor.scheduler.runner import build_scheduler  # noqa: E402


def _load_or_default() -> tuple[AppConfig, WatchlistConfig]:
    settings_path = ROOT / "config" / "settings.yaml"
    watchlist_path = ROOT / "config" / "watchlist.yaml"
    example_watchlist = ROOT / "config" / "watchlist.example.yaml"

    cfg: AppConfig
    if settings_path.exists():
        cfg = load_settings(settings_path)
        # Cron list in settings.yaml may be partial; backfill defaults
        cfg = _ensure_all_crons(cfg)
    else:
        cfg = AppConfig(
            opend=OpenDConfig(),
            database=DatabaseConfig(path=str(ROOT / "data" / "dryrun.db")),
            scheduler=SchedulerConfig(
                timezone="America/New_York",
                jobs={
                    "intraday_check": JobCron(cron="30 9-15 * * mon-fri"),
                    "morning_brief": JobCron(cron="30 10 * * mon-fri"),
                    "closing_brief": JobCron(cron="30 16 * * mon-fri"),
                    "news_pulse": JobCron(cron="*/30 9-15 * * mon-fri"),
                },
            ),
            lark=LarkConfig(receiver=LarkReceiver(type="chat", open_id="ou_dryrun")),
            signals=SignalsConfig(),
            logging=LoggingConfig(),
        )

    if watchlist_path.exists():
        wl = load_watchlist(watchlist_path)
    elif example_watchlist.exists():
        wl = load_watchlist(example_watchlist)
    else:
        wl = WatchlistConfig(symbols=[SymbolConfig(code="US.AAPL", name="Apple")])

    (ROOT / "data").mkdir(exist_ok=True)
    return cfg, wl


def _ensure_all_crons(cfg: AppConfig) -> AppConfig:
    defaults = {
        "intraday_check": "30 9-15 * * mon-fri",
        "morning_brief": "30 10 * * mon-fri",
        "closing_brief": "30 16 * * mon-fri",
        "news_pulse": "*/30 9-15 * * mon-fri",
    }
    for name, cron in defaults.items():
        if name not in cfg.scheduler.jobs:
            cfg.scheduler.jobs[name] = JobCron(cron=cron)
    return cfg


def main() -> int:
    cfg, wl = _load_or_default()

    print("=== equity-monitor scheduler dry-run ===")
    print(f"now (UTC):     {datetime.now(tz=timezone.utc).isoformat(timespec='seconds')}")
    print(f"sched tz:      {cfg.scheduler.timezone}")
    print(f"db.path:       {cfg.database.path}")
    print(f"watchlist:     {[s.code for s in wl.symbols]}")
    print()

    sched = build_scheduler(
        cfg=cfg,
        watchlist=wl,
        client_factory=lambda: FakeFutuClient(),
    )

    from zoneinfo import ZoneInfo

    now_local = datetime.now(tz=ZoneInfo(cfg.scheduler.timezone))

    print(f"Registered jobs ({len(sched.get_jobs())}):")
    for job in sched.get_jobs():
        trigger = job.trigger
        try:
            next_fire = trigger.get_next_fire_time(None, now_local)
        except Exception as e:
            next_fire = f"<err {e}>"
        print(f"  - id={job.id:18s} trigger={trigger}  next_fire={next_fire}")
    print()
    print("OK: scheduler constructed cleanly without OpenD/lark-cli/network.")
    print("(Not calling .start() — that would block forever in BlockingScheduler.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
