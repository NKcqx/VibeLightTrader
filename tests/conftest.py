from __future__ import annotations

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

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
