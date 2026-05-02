from __future__ import annotations

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from equity_monitor.db import init_schema, make_engine, make_sessionmaker


@pytest.fixture
def engine(tmp_path) -> Engine:
    db = tmp_path / "test.db"
    eng = make_engine(db, wal_mode=False)
    init_schema(eng)
    return eng


@pytest.fixture
def factory(engine: Engine) -> sessionmaker[Session]:
    return make_sessionmaker(engine)
