from __future__ import annotations

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from equity_monitor.config import (
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
)
from equity_monitor.db import init_schema, make_engine, make_sessionmaker
from equity_monitor.futu_client import FakeFutuClient


@pytest.fixture
def engine(tmp_path) -> Engine:
    db = tmp_path / "test.db"
    eng = make_engine(db, wal_mode=False)
    init_schema(eng)
    return eng


@pytest.fixture
def factory(engine: Engine) -> sessionmaker[Session]:
    return make_sessionmaker(engine)


@pytest.fixture
def fake_futu() -> FakeFutuClient:
    return FakeFutuClient()


@pytest.fixture
def app_cfg(tmp_path) -> AppConfig:
    return AppConfig(
        opend=OpenDConfig(),
        database=DatabaseConfig(path=str(tmp_path / "test.db")),
        scheduler=SchedulerConfig(
            timezone="America/New_York",
            jobs={
                "intraday_check": JobCron(cron="30 9-15 * * mon-fri"),
                "morning_brief": JobCron(cron="30 10 * * mon-fri"),
                "closing_brief": JobCron(cron="30 16 * * mon-fri"),
                "news_pulse": JobCron(cron="*/30 9-15 * * mon-fri"),
            },
        ),
        lark=LarkConfig(receiver=LarkReceiver(type="chat", open_id="ou_test")),
        signals=SignalsConfig(),
        logging=LoggingConfig(),
    )


@pytest.fixture
def watchlist() -> WatchlistConfig:
    return WatchlistConfig(
        symbols=[
            SymbolConfig(
                code="US.AAPL",
                name="Apple",
                upper_threshold=200.0,
                lower_threshold=165.0,
            )
        ]
    )
