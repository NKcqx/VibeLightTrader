from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from equity_monitor.db import init_schema, make_engine, make_sessionmaker, session_scope
from equity_monitor.events.apply import HELP_TEXT, apply
from equity_monitor.events.grammar import (
    AddCommand,
    HelpCommand,
    ListCommand,
    RemoveCommand,
    ThresholdCommand,
)
from equity_monitor.models import Symbol


@pytest.fixture
def factory(tmp_path: Path) -> sessionmaker:
    engine = make_engine(str(tmp_path / "x.db"), wal_mode=False)
    init_schema(engine)
    return make_sessionmaker(engine)


def test_help_returns_text(factory: sessionmaker) -> None:
    out = apply(HelpCommand(), factory)
    assert out == HELP_TEXT
    assert "添加" in out and "删除" in out


def test_list_empty(factory: sessionmaker) -> None:
    out = apply(ListCommand(), factory)
    assert "监控列表为空" in out


def test_add_creates_row(factory: sessionmaker) -> None:
    out = apply(AddCommand(code="US.AAPL", upper=200.0, lower=165.0), factory)
    assert "已添加" in out
    assert "US.AAPL" in out
    assert "200" in out and "165" in out
    with session_scope(factory) as s:
        row = s.query(Symbol).filter(Symbol.code == "US.AAPL").one()
        assert row.upper_threshold == 200.0
        assert row.lower_threshold == 165.0


def test_add_without_thresholds(factory: sessionmaker) -> None:
    out = apply(AddCommand(code="US.TSLA"), factory)
    assert "已添加" in out
    with session_scope(factory) as s:
        row = s.query(Symbol).filter(Symbol.code == "US.TSLA").one()
        assert row.upper_threshold is None
        assert row.lower_threshold is None


def test_add_existing_updates_thresholds(factory: sessionmaker) -> None:
    apply(AddCommand(code="US.AAPL", upper=200.0, lower=165.0), factory)
    out = apply(AddCommand(code="US.AAPL", upper=210.0), factory)
    assert "已更新" in out and "上限→210" in out
    with session_scope(factory) as s:
        row = s.query(Symbol).filter(Symbol.code == "US.AAPL").one()
        assert row.upper_threshold == 210.0
        # lower remains untouched
        assert row.lower_threshold == 165.0


def test_add_existing_no_changes_message(factory: sessionmaker) -> None:
    apply(AddCommand(code="US.AAPL", upper=200.0, lower=165.0), factory)
    out = apply(AddCommand(code="US.AAPL", upper=200.0, lower=165.0), factory)
    assert "已在监控中" in out


def test_list_after_add_shows_rows(factory: sessionmaker) -> None:
    apply(AddCommand(code="US.AAPL", upper=200.0, lower=165.0, name="Apple"), factory)
    apply(AddCommand(code="US.NVDA"), factory)
    out = apply(ListCommand(), factory)
    assert "US.AAPL" in out
    assert "US.NVDA" in out
    assert "Apple" in out
    assert "200" in out and "165" in out


def test_remove_existing(factory: sessionmaker) -> None:
    apply(AddCommand(code="US.AAPL"), factory)
    out = apply(RemoveCommand(code="US.AAPL"), factory)
    assert "已从监控列表移除" in out
    with session_scope(factory) as s:
        assert s.query(Symbol).count() == 0


def test_remove_unknown(factory: sessionmaker) -> None:
    out = apply(RemoveCommand(code="US.AAPL"), factory)
    assert "不在监控列表中" in out


def test_threshold_update(factory: sessionmaker) -> None:
    apply(AddCommand(code="US.AAPL", upper=200.0, lower=165.0), factory)
    out = apply(ThresholdCommand(code="US.AAPL", upper=210.0), factory)
    assert "上限→210" in out
    with session_scope(factory) as s:
        row = s.query(Symbol).filter(Symbol.code == "US.AAPL").one()
        assert row.upper_threshold == 210.0
        assert row.lower_threshold == 165.0


def test_threshold_for_unknown_symbol(factory: sessionmaker) -> None:
    out = apply(ThresholdCommand(code="US.AAPL", upper=210.0), factory)
    assert "不在监控列表中" in out


def test_threshold_no_values_no_op(factory: sessionmaker) -> None:
    apply(AddCommand(code="US.AAPL", upper=200.0, lower=165.0), factory)
    out = apply(ThresholdCommand(code="US.AAPL"), factory)
    assert "未变更" in out
