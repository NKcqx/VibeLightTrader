"""Unit tests for journal/metrics.py — decision-quality hit-rate windows."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from equity_monitor.journal.metrics import (
    compute_hit_rates,
    render_hit_rate_lines,
)
from equity_monitor.models import Base, Quote, Symbol


@pytest.fixture()
def factory(tmp_path: Path) -> sessionmaker:
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture()
def audit_log(tmp_path: Path) -> Path:
    return tmp_path / "decisions.jsonl"


def _write_decision(
    audit_log: Path, *, ts: datetime, code: str, action: str, fallback: bool = False
) -> None:
    row = {
        "ts_unix": ts.replace(tzinfo=timezone.utc).timestamp() if ts.tzinfo is None
        else ts.timestamp(),
        "code": code,
        "client": "test",
        "model": "test",
        "decision": {"action": action, "qty": 50, "reason": "x"},
        "fallback_used": fallback,
    }
    with audit_log.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def _seed_symbol_and_quote(
    factory: sessionmaker, *, code: str, ts: datetime, close: float
) -> None:
    with factory() as session:
        sym = session.query(Symbol).filter(Symbol.code == code).one_or_none()
        if sym is None:
            sym = Symbol(code=code, name=code.split(".")[-1])
            session.add(sym)
            session.flush()
        ts_naive = ts.replace(tzinfo=None) if ts.tzinfo is not None else ts
        session.add(
            Quote(
                symbol_id=sym.id,
                ts=ts_naive,
                open=close, high=close, low=close, close=close,
                volume=0, turnover=0.0,
            )
        )
        session.commit()


# ---------------------------------------------------------------------------


def test_no_audit_log_returns_pending_zero_evaluated(factory, tmp_path):
    audit = tmp_path / "no_such_file.jsonl"
    cutoff = datetime(2026, 5, 4, 14, 0, tzinfo=timezone.utc)
    stats = compute_hit_rates(
        audit_log_path=audit, factory=factory, code="US.NVDA", cutoff=cutoff
    )
    assert len(stats) == 2  # 7d + 30d windows
    for s in stats:
        assert s.actionable_total == 0
        assert s.evaluated == 0
        assert s.hit_rate is None


def test_hold_decisions_excluded_from_actionable(factory, audit_log):
    cutoff = datetime(2026, 5, 4, 14, 0, tzinfo=timezone.utc)
    _write_decision(audit_log, ts=cutoff - timedelta(days=2),
                    code="US.NVDA", action="HOLD")
    _write_decision(audit_log, ts=cutoff - timedelta(days=1),
                    code="US.NVDA", action="HOLD")
    stats = compute_hit_rates(
        audit_log_path=audit_log, factory=factory, code="US.NVDA", cutoff=cutoff
    )
    seven_d = stats[0]
    assert seven_d.decisions_total == 2
    assert seven_d.actionable_total == 0
    assert seven_d.hit_rate is None


def test_buy_hit_when_price_rises(factory, audit_log):
    cutoff = datetime(2026, 5, 4, 14, 0, tzinfo=timezone.utc)
    decision_ts = cutoff - timedelta(days=3)

    _seed_symbol_and_quote(factory, code="US.NVDA",
                           ts=decision_ts, close=190.0)
    _seed_symbol_and_quote(factory, code="US.NVDA",
                           ts=decision_ts + timedelta(days=1), close=195.0)
    _write_decision(audit_log, ts=decision_ts, code="US.NVDA", action="BUY")

    stats = compute_hit_rates(
        audit_log_path=audit_log, factory=factory, code="US.NVDA", cutoff=cutoff
    )
    seven_d = stats[0]
    assert seven_d.actionable_total == 1
    assert seven_d.evaluated == 1
    assert seven_d.hits == 1
    assert seven_d.misses == 0
    assert seven_d.hit_rate == 1.0


def test_sell_miss_when_price_rises(factory, audit_log):
    cutoff = datetime(2026, 5, 4, 14, 0, tzinfo=timezone.utc)
    decision_ts = cutoff - timedelta(days=3)

    _seed_symbol_and_quote(factory, code="US.NVDA",
                           ts=decision_ts, close=190.0)
    _seed_symbol_and_quote(factory, code="US.NVDA",
                           ts=decision_ts + timedelta(days=1), close=195.0)
    _write_decision(audit_log, ts=decision_ts, code="US.NVDA", action="SELL")

    stats = compute_hit_rates(
        audit_log_path=audit_log, factory=factory, code="US.NVDA", cutoff=cutoff
    )
    assert stats[0].misses == 1
    assert stats[0].hits == 0
    assert stats[0].hit_rate == 0.0


def test_eval_window_not_passed_counted_pending(factory, audit_log):
    cutoff = datetime(2026, 5, 4, 14, 0, tzinfo=timezone.utc)
    # Decision happened 12 hours ago — eval_after_days=1 hasn't elapsed.
    decision_ts = cutoff - timedelta(hours=12)
    _seed_symbol_and_quote(factory, code="US.NVDA",
                           ts=decision_ts, close=190.0)
    _write_decision(audit_log, ts=decision_ts, code="US.NVDA", action="BUY")

    stats = compute_hit_rates(
        audit_log_path=audit_log, factory=factory, code="US.NVDA", cutoff=cutoff
    )
    seven_d = stats[0]
    assert seven_d.actionable_total == 1
    assert seven_d.evaluated == 0
    assert seven_d.pending == 1
    assert seven_d.hit_rate is None


def test_decisions_for_other_codes_ignored(factory, audit_log):
    cutoff = datetime(2026, 5, 4, 14, 0, tzinfo=timezone.utc)
    decision_ts = cutoff - timedelta(days=3)

    _seed_symbol_and_quote(factory, code="US.MSFT",
                           ts=decision_ts, close=400.0)
    _seed_symbol_and_quote(factory, code="US.MSFT",
                           ts=decision_ts + timedelta(days=1), close=410.0)
    _write_decision(audit_log, ts=decision_ts, code="US.MSFT", action="BUY")

    stats = compute_hit_rates(
        audit_log_path=audit_log, factory=factory, code="US.NVDA", cutoff=cutoff
    )
    assert stats[0].actionable_total == 0
    assert stats[0].decisions_total == 0


def test_corrupt_lines_ignored(factory, audit_log):
    cutoff = datetime(2026, 5, 4, 14, 0, tzinfo=timezone.utc)
    decision_ts = cutoff - timedelta(days=3)

    audit_log.write_text(
        "this is not json\n"
        '{"ts_unix": "still-not-valid-types"}\n'
        "\n"
        + json.dumps({
            "ts_unix": decision_ts.timestamp(),
            "code": "US.NVDA",
            "decision": {"action": "BUY", "qty": 50, "reason": "y"},
            "fallback_used": False,
        }) + "\n",
        encoding="utf-8",
    )
    _seed_symbol_and_quote(factory, code="US.NVDA",
                           ts=decision_ts, close=190.0)
    _seed_symbol_and_quote(factory, code="US.NVDA",
                           ts=decision_ts + timedelta(days=1), close=180.0)

    stats = compute_hit_rates(
        audit_log_path=audit_log, factory=factory, code="US.NVDA", cutoff=cutoff
    )
    assert stats[0].actionable_total == 1
    assert stats[0].misses == 1


def test_render_lines_no_decisions_shows_dash(factory, audit_log):
    cutoff = datetime(2026, 5, 4, 14, 0, tzinfo=timezone.utc)
    stats = compute_hit_rates(
        audit_log_path=audit_log, factory=factory, code="US.NVDA", cutoff=cutoff
    )
    rendered = render_hit_rate_lines(stats)
    assert any("近 7 天决策胜率" in line for line in rendered)
    assert all("—" in line for line in rendered)
    # When there are zero rows AT ALL we should NOT claim "仅有 HOLD".
    assert all("尚无任何决策" in line for line in rendered)


def test_render_lines_only_hold_decisions_says_hold_only(factory, audit_log):
    cutoff = datetime(2026, 5, 4, 14, 0, tzinfo=timezone.utc)
    decision_ts = cutoff - timedelta(days=2)
    _write_decision(audit_log, ts=decision_ts, code="US.NVDA", action="HOLD")
    _write_decision(audit_log, ts=decision_ts, code="US.NVDA", action="HOLD")
    stats = compute_hit_rates(
        audit_log_path=audit_log, factory=factory, code="US.NVDA", cutoff=cutoff
    )
    rendered = render_hit_rate_lines(stats)
    assert any("仅有 2 次 HOLD" in line for line in rendered)


def test_render_lines_evaluated_shows_percentage(factory, audit_log):
    cutoff = datetime(2026, 5, 4, 14, 0, tzinfo=timezone.utc)
    decision_ts = cutoff - timedelta(days=3)
    _seed_symbol_and_quote(factory, code="US.NVDA",
                           ts=decision_ts, close=190.0)
    _seed_symbol_and_quote(factory, code="US.NVDA",
                           ts=decision_ts + timedelta(days=1), close=195.0)
    _write_decision(audit_log, ts=decision_ts, code="US.NVDA", action="BUY")
    stats = compute_hit_rates(
        audit_log_path=audit_log, factory=factory, code="US.NVDA", cutoff=cutoff
    )
    rendered = render_hit_rate_lines(stats)
    assert any("100%" in line and "(1/1)" in line for line in rendered)
