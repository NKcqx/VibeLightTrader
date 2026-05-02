from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from equity_monitor.cli.main import cli
from equity_monitor.db import make_engine, make_sessionmaker, session_scope
from equity_monitor.models import Position, Symbol
from equity_monitor.models import Signal as SignalRow
from equity_monitor.trader.paper import FakePaperTrader


@pytest.fixture
def cli_root(tmp_path: Path) -> Path:
    (tmp_path / "config").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "config" / "settings.yaml").write_text(
        yaml.safe_dump(
            {
                "opend": {"host": "127.0.0.1", "port": 11111},
                "database": {
                    "path": str(tmp_path / "data" / "x.db"),
                    "wal_mode": False,
                },
                "scheduler": {
                    "timezone": "America/New_York",
                    "jobs": {
                        "intraday_check": {"cron": "30 9-15 * * mon-fri"},
                        "morning_brief": {"cron": "30 10 * * mon-fri"},
                        "closing_brief": {"cron": "30 16 * * mon-fri"},
                        "news_pulse": {"cron": "*/30 9-15 * * mon-fri"},
                    },
                },
                "lark": {
                    "cli_path": "lark-cli",
                    "identity": "bot",
                    "receiver": {"type": "user", "open_id": "ou_test"},
                },
                "signals": {},
                "logging": {"level": "INFO"},
            }
        )
    )
    (tmp_path / "config" / "watchlist.yaml").write_text(
        yaml.safe_dump(
            {
                "symbols": [
                    {
                        "code": "US.AAPL",
                        "name": "Apple",
                        "upper_threshold": 200,
                        "lower_threshold": 165,
                    }
                ]
            }
        )
    )
    return tmp_path


def _base_args(cli_root: Path) -> list[str]:
    return [
        "--settings",
        str(cli_root / "config" / "settings.yaml"),
        "--watchlist",
        str(cli_root / "config" / "watchlist.yaml"),
    ]


def _seed_pending_signal(
    db_path: Path,
    *,
    code: str = "US.AAPL",
    action: str = "BUY",
    qty: int = 100,
) -> int:
    """Create a Symbol + a pending signal with suggested_action; return signal id."""
    engine = make_engine(str(db_path), wal_mode=False)
    factory = make_sessionmaker(engine)
    with session_scope(factory) as s:
        sym = s.query(Symbol).filter(Symbol.code == code).one_or_none()
        if sym is None:
            sym = Symbol(code=code, name=code.split(".")[-1])
            s.add(sym)
            s.flush()
        sig = SignalRow(
            symbol_id=sym.id,
            ts=datetime.now(tz=timezone.utc),
            signal_type="threshold_breach_lower",
            severity="CRITICAL",
            payload_json="{}",
            suggested_action=action,
            suggested_qty=qty,
            status="pending",
        )
        s.add(sig)
        s.flush()
        return sig.id


def test_trade_list_empty(cli_root: Path) -> None:
    runner = CliRunner()
    runner.invoke(cli, _base_args(cli_root) + ["db", "init"])
    result = runner.invoke(cli, _base_args(cli_root) + ["trade", "list"])
    assert result.exit_code == 0, result.output
    assert "no signals" in result.output


def test_trade_list_pending_shows_id_and_action(cli_root: Path) -> None:
    runner = CliRunner()
    runner.invoke(cli, _base_args(cli_root) + ["db", "init"])
    sig_id = _seed_pending_signal(cli_root / "data" / "x.db", action="BUY", qty=100)
    result = runner.invoke(cli, _base_args(cli_root) + ["trade", "list"])
    assert result.exit_code == 0, result.output
    assert "US.AAPL" in result.output
    assert "BUY" in result.output
    assert str(sig_id) in result.output
    assert "100" in result.output


def test_trade_confirm_places_order_and_persists(cli_root: Path, monkeypatch) -> None:
    runner = CliRunner()
    runner.invoke(cli, _base_args(cli_root) + ["db", "init"])
    sig_id = _seed_pending_signal(cli_root / "data" / "x.db", action="BUY", qty=10)

    fake = FakePaperTrader()
    fake.set_mark("US.AAPL", 178.5)
    monkeypatch.setattr("equity_monitor.cli.main._make_trader", lambda cfg: fake)

    result = runner.invoke(
        cli, _base_args(cli_root) + ["trade", "confirm", str(sig_id)]
    )
    assert result.exit_code == 0, result.output
    assert "placed paper order" in result.output
    assert "BUY 10 US.AAPL" in result.output

    # broker side
    pos = fake.query_positions()
    assert len(pos) == 1 and pos[0].qty == 10

    # DB side
    engine = make_engine(str(cli_root / "data" / "x.db"), wal_mode=False)
    factory = make_sessionmaker(engine)
    with session_scope(factory) as s:
        sig = s.query(SignalRow).filter(SignalRow.id == sig_id).one()
        assert sig.status == "executed"
        assert sig.executed_trade_id is not None
        db_pos = s.query(Position).filter(Position.qty > 0).one()
        assert db_pos.qty == 10
        assert db_pos.avg_cost == 178.5


def test_trade_confirm_idempotent_on_already_executed(cli_root: Path, monkeypatch) -> None:
    runner = CliRunner()
    runner.invoke(cli, _base_args(cli_root) + ["db", "init"])
    sig_id = _seed_pending_signal(cli_root / "data" / "x.db", action="BUY", qty=5)

    fake = FakePaperTrader()
    fake.set_mark("US.AAPL", 180.0)
    monkeypatch.setattr("equity_monitor.cli.main._make_trader", lambda cfg: fake)

    runner.invoke(cli, _base_args(cli_root) + ["trade", "confirm", str(sig_id)])
    result = runner.invoke(
        cli, _base_args(cli_root) + ["trade", "confirm", str(sig_id)]
    )
    assert result.exit_code == 0, result.output
    assert "already executed" in result.output
    # no double-buy
    assert fake.query_positions()[0].qty == 5


def test_trade_confirm_cancel_path_when_broker_rejects(
    cli_root: Path, monkeypatch
) -> None:
    runner = CliRunner()
    runner.invoke(cli, _base_args(cli_root) + ["db", "init"])
    sig_id = _seed_pending_signal(cli_root / "data" / "x.db", action="BUY", qty=10)

    fake = FakePaperTrader()  # no mark price → REJECTED
    monkeypatch.setattr("equity_monitor.cli.main._make_trader", lambda cfg: fake)

    result = runner.invoke(
        cli, _base_args(cli_root) + ["trade", "confirm", str(sig_id)]
    )
    assert result.exit_code != 0
    assert "rejected" in result.output


def test_trade_confirm_qty_override(cli_root: Path, monkeypatch) -> None:
    runner = CliRunner()
    runner.invoke(cli, _base_args(cli_root) + ["db", "init"])
    sig_id = _seed_pending_signal(cli_root / "data" / "x.db", action="BUY", qty=100)

    fake = FakePaperTrader()
    fake.set_mark("US.AAPL", 180.0)
    monkeypatch.setattr("equity_monitor.cli.main._make_trader", lambda cfg: fake)

    result = runner.invoke(
        cli, _base_args(cli_root) + ["trade", "confirm", str(sig_id), "--qty", "25"]
    )
    assert result.exit_code == 0, result.output
    assert fake.query_positions()[0].qty == 25  # not 100


def test_trade_cancel_marks_status(cli_root: Path) -> None:
    runner = CliRunner()
    runner.invoke(cli, _base_args(cli_root) + ["db", "init"])
    sig_id = _seed_pending_signal(cli_root / "data" / "x.db")

    result = runner.invoke(
        cli, _base_args(cli_root) + ["trade", "cancel", str(sig_id)]
    )
    assert result.exit_code == 0, result.output

    engine = make_engine(str(cli_root / "data" / "x.db"), wal_mode=False)
    factory = make_sessionmaker(engine)
    with session_scope(factory) as s:
        sig = s.query(SignalRow).filter(SignalRow.id == sig_id).one()
        assert sig.status == "cancelled"

    # cancelling again → error (only pending can be cancelled)
    result2 = runner.invoke(
        cli, _base_args(cli_root) + ["trade", "cancel", str(sig_id)]
    )
    assert result2.exit_code != 0


def test_trade_positions_shows_open(cli_root: Path, monkeypatch) -> None:
    runner = CliRunner()
    runner.invoke(cli, _base_args(cli_root) + ["db", "init"])
    sig_id = _seed_pending_signal(cli_root / "data" / "x.db", action="BUY", qty=12)
    fake = FakePaperTrader()
    fake.set_mark("US.AAPL", 178.0)
    monkeypatch.setattr("equity_monitor.cli.main._make_trader", lambda cfg: fake)
    runner.invoke(cli, _base_args(cli_root) + ["trade", "confirm", str(sig_id)])

    result = runner.invoke(cli, _base_args(cli_root) + ["trade", "positions"])
    assert result.exit_code == 0, result.output
    assert "US.AAPL" in result.output
    assert "12" in result.output


def test_trade_pnl_realized_after_buy_then_sell(cli_root: Path, monkeypatch) -> None:
    runner = CliRunner()
    runner.invoke(cli, _base_args(cli_root) + ["db", "init"])

    fake = FakePaperTrader()
    fake.close = lambda: None  # type: ignore[method-assign]
    fake.set_mark("US.AAPL", 100.0)
    monkeypatch.setattr("equity_monitor.cli.main._make_trader", lambda cfg: fake)

    buy_id = _seed_pending_signal(cli_root / "data" / "x.db", action="BUY", qty=10)
    r1 = runner.invoke(cli, _base_args(cli_root) + ["trade", "confirm", str(buy_id)])
    assert r1.exit_code == 0, r1.output

    sell_id = _seed_pending_signal(cli_root / "data" / "x.db", action="SELL", qty=10)
    fake.set_mark("US.AAPL", 130.0)
    r2 = runner.invoke(cli, _base_args(cli_root) + ["trade", "confirm", str(sell_id)])
    assert r2.exit_code == 0, r2.output

    result = runner.invoke(cli, _base_args(cli_root) + ["trade", "pnl"])
    assert result.exit_code == 0, result.output
    # realized = (130 - 100) * 10 = +300
    assert "+300" in result.output


def test_trade_help_shows_subcommands() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["trade", "--help"])
    assert result.exit_code == 0
    for sub in ("list", "confirm", "cancel", "positions", "pnl"):
        assert sub in result.output
