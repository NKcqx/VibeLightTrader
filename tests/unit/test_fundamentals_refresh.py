"""Step 1 — refresh path: ``refresh_fixtures`` + ``run_refresh_fundamentals``.

Both use a stub fetcher; nothing here touches yfinance over the wire.
Covers atomic write semantics, per-symbol failure isolation, the
"non-US is skipped" rule, and the runner-level wrapper that drives the
cron job.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from vibe_trader.config import (
    AppConfig,
    DatabaseConfig,
    FundamentalsConfig,
    JobCron,
    LarkConfig,
    LarkReceiver,
    LoggingConfig,
    OpenDConfig,
    SchedulerConfig,
    SignalsConfig,
    SymbolConfig,
    WatchlistConfig,
)
from vibe_trader.data.fundamentals import refresh_fixtures
from vibe_trader.scheduler.jobs import run_refresh_fundamentals


def _stub_fetcher_ok(t: str) -> dict[str, Any]:
    return {
        "ticker": t,
        "code": f"US.{t}",
        "fetched_at": "2026-05-07T00:00:00+00:00",
        "info": {"currentPrice": 100.0, "recommendationKey": "buy"},
        "recommendations": [],
        "upgrades_downgrades": [],
        "news": [],
        "calendar": {},
    }


def _stub_fetcher_boom(_: str) -> dict[str, Any]:
    raise RuntimeError("simulated yfinance 503")


# ---------------------------------------------------------------------------
# refresh_fixtures.
# ---------------------------------------------------------------------------


def test_refresh_fixtures_writes_files(tmp_path: Path) -> None:
    summary = refresh_fixtures(
        ["US.NVDA", "US.MSFT"], fixture_dir=tmp_path, fetcher=_stub_fetcher_ok
    )
    assert summary == {"US.NVDA": "ok", "US.MSFT": "ok"}
    nvda = json.loads((tmp_path / "US.NVDA.json").read_text())
    assert nvda["code"] == "US.NVDA"
    assert nvda["info"]["recommendationKey"] == "buy"


def test_refresh_fixtures_skips_non_us(tmp_path: Path) -> None:
    summary = refresh_fixtures(
        ["HK.00700", "SH.600519", "US.NVDA"],
        fixture_dir=tmp_path,
        fetcher=_stub_fetcher_ok,
    )
    assert summary["HK.00700"] == "skipped:non-US"
    assert summary["SH.600519"] == "skipped:non-US"
    assert summary["US.NVDA"] == "ok"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["US.NVDA.json"]


def test_refresh_fixtures_per_symbol_failure_isolated(tmp_path: Path) -> None:
    calls: list[str] = []

    def mixed(t: str) -> dict[str, Any]:
        calls.append(t)
        if t == "MSFT":
            raise RuntimeError("network blip")
        return _stub_fetcher_ok(t)

    summary = refresh_fixtures(
        ["US.NVDA", "US.MSFT", "US.AAPL"], fixture_dir=tmp_path, fetcher=mixed
    )
    assert summary["US.NVDA"] == "ok"
    assert summary["US.MSFT"].startswith("error:RuntimeError")
    assert summary["US.AAPL"] == "ok"
    # Only the successful ones are on disk.
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "US.AAPL.json",
        "US.NVDA.json",
    ]


def test_refresh_fixtures_overwrites_existing(tmp_path: Path) -> None:
    out = tmp_path / "US.NVDA.json"
    out.write_text('{"old": true}')
    refresh_fixtures(["US.NVDA"], fixture_dir=tmp_path, fetcher=_stub_fetcher_ok)
    assert "currentPrice" in out.read_text()
    assert "old" not in out.read_text()


def test_refresh_fixtures_atomic_no_tmp_leak_on_success(tmp_path: Path) -> None:
    refresh_fixtures(["US.NVDA"], fixture_dir=tmp_path, fetcher=_stub_fetcher_ok)
    assert not any(p.suffix == ".tmp" for p in tmp_path.iterdir())


# ---------------------------------------------------------------------------
# run_refresh_fundamentals (cron wrapper).
# ---------------------------------------------------------------------------


def _make_cfg(*, source: str = "fixture", fixture_dir: str | None = None) -> AppConfig:
    return AppConfig(
        opend=OpenDConfig(),
        database=DatabaseConfig(path="data/x.db"),
        scheduler=SchedulerConfig(
            timezone="America/New_York",
            jobs={
                "intraday_check": JobCron(cron="30 9 * * mon-fri"),
                "morning_brief": JobCron(cron="30 10 * * mon-fri"),
                "closing_brief": JobCron(cron="30 16 * * mon-fri"),
                "refresh_fundamentals": JobCron(cron="0 6 * * *"),
            },
        ),
        lark=LarkConfig(receiver=LarkReceiver(type="user", open_id="ou_x")),
        signals=SignalsConfig(),
        logging=LoggingConfig(),
        fundamentals=FundamentalsConfig(source=source, fixture_dir=fixture_dir),
    )


def _wl(*codes: str) -> WatchlistConfig:
    return WatchlistConfig(
        symbols=[SymbolConfig(code=c, name=c.split(".", 1)[1]) for c in codes]
    )


def test_run_refresh_fundamentals_writes_for_us_codes(tmp_path: Path) -> None:
    cfg = _make_cfg(fixture_dir=str(tmp_path))
    wl = _wl("US.NVDA", "US.MSFT")
    with patch(
        "vibe_trader.data.fundamentals.refresh_fixtures",
        wraps=lambda codes, **kw: refresh_fixtures(
            codes, fetcher=_stub_fetcher_ok, **kw
        ),
    ):
        summary = run_refresh_fundamentals(cfg=cfg, watchlist=wl)
    assert summary == {"US.NVDA": "ok", "US.MSFT": "ok"}
    assert (tmp_path / "US.NVDA.json").exists()


def test_run_refresh_fundamentals_skipped_when_source_none(
    tmp_path: Path,
) -> None:
    cfg = _make_cfg(source="none", fixture_dir=str(tmp_path))
    wl = _wl("US.NVDA")
    summary = run_refresh_fundamentals(cfg=cfg, watchlist=wl)
    assert summary == {}
    assert not list(tmp_path.iterdir())


def test_run_refresh_fundamentals_uses_yfinance_fetcher_by_default(
    tmp_path: Path,
) -> None:
    """Smoke-check that the live path is wired up — but never actually called."""
    cfg = _make_cfg(fixture_dir=str(tmp_path))
    wl = _wl("US.NVDA")
    called: list[str] = []

    def fake_yf(t: str) -> dict[str, Any]:
        called.append(t)
        return _stub_fetcher_ok(t)

    with patch(
        "vibe_trader.data.fundamentals_yfinance.fetch_raw_fundamentals", fake_yf
    ):
        summary = run_refresh_fundamentals(cfg=cfg, watchlist=wl)
    assert called == ["NVDA"]
    assert summary["US.NVDA"] == "ok"


# ---------------------------------------------------------------------------
# scheduler/runner registration.
# ---------------------------------------------------------------------------


def test_runner_registers_refresh_job_when_configured(tmp_path: Path) -> None:
    from vibe_trader.scheduler.runner import build_scheduler

    cfg = _make_cfg(fixture_dir=str(tmp_path))
    cfg.database.path = str(tmp_path / "x.db")  # avoid root-relative path
    wl = _wl("US.NVDA")

    class _FakeClient:
        def close(self) -> None: ...

    sched = build_scheduler(
        cfg=cfg, watchlist=wl, client_factory=lambda: _FakeClient()
    )
    ids = {j.id for j in sched.get_jobs()}
    assert "refresh_fundamentals" in ids
    assert {"intraday_check", "morning_brief", "closing_brief"}.issubset(ids)


def test_runner_omits_refresh_job_when_unconfigured(tmp_path: Path) -> None:
    from vibe_trader.scheduler.runner import build_scheduler

    cfg = _make_cfg(fixture_dir=str(tmp_path))
    cfg.scheduler.jobs.pop("refresh_fundamentals")
    cfg.database.path = str(tmp_path / "x.db")
    wl = _wl("US.NVDA")

    class _FakeClient:
        def close(self) -> None: ...

    sched = build_scheduler(
        cfg=cfg, watchlist=wl, client_factory=lambda: _FakeClient()
    )
    ids = {j.id for j in sched.get_jobs()}
    assert "refresh_fundamentals" not in ids
