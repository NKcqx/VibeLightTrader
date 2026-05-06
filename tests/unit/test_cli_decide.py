"""End-to-end CLI tests for the HITL decide command group.

Covers list / show / submit / cancel and the full
pending → submitted → executed lifecycle including the actual paper
trade insertion into the SQLite DB via FakePaperTrader.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from vibe_trader.cli.main import cli
from vibe_trader.db import make_engine, make_sessionmaker, session_scope
from vibe_trader.decisions.packet import build_packet
from vibe_trader.decisions.store import PacketState, PacketStore
from vibe_trader.models import Position, Symbol
from vibe_trader.models import Signal as SignalRow
from vibe_trader.models import Trade
from vibe_trader.signals.base import Severity, Signal
from vibe_trader.signals.strategy_base import StrategyContext
from vibe_trader.trader.paper import FakePaperTrader


# ---------------------------------------------------------------------------
# Test config — minimal repo layout under tmp_path with a hitl var_dir
# pointed at tmp_path/var/decisions so packets don't pollute the real one.
# ---------------------------------------------------------------------------


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
                "trader": {
                    "auto_execute": True,
                    "strategy": {
                        "type": "rule",  # don't enable hitl for these unit tests
                        "hitl": {
                            "var_dir": str(tmp_path / "var" / "decisions"),
                            "max_position_per_symbol": 200,
                            "min_trade_size": 10,
                            "min_confidence": 0.6,
                        },
                    },
                },
            }
        )
    )
    (tmp_path / "config" / "watchlist.yaml").write_text(
        yaml.safe_dump(
            {
                "symbols": [
                    {
                        "code": "US.NVDA",
                        "name": "NVIDIA",
                        "upper_threshold": 1000,
                        "lower_threshold": 500,
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


def _seed_packet(cli_root: Path, *, code: str = "US.NVDA") -> str:
    """Drop a pending packet straight onto disk; return its id."""
    var_dir = cli_root / "var" / "decisions"
    store = PacketStore(var_dir)
    sig = Signal(
        code=code,
        ts=datetime.now(tz=timezone.utc),
        signal_type="rsi_oversold",
        severity=Severity.WARN,
        payload={"rsi": 28.0, "close": 850.0},
    )
    ctx = StrategyContext(
        code=code,
        signals=[sig],
        position_qty=0,
        avg_cost=0.0,
        realized_pnl=0.0,
    )
    p = build_packet(
        ctx,
        triggering_signal_ids=[],
        constraints={"max_position": 200, "min_trade_size": 10, "min_confidence": 0.6},
    )
    store.write_pending(p)
    return p.id


# ---------------------------------------------------------------------------
# list / show / cancel.
# ---------------------------------------------------------------------------


def test_decide_list_empty(cli_root: Path) -> None:
    runner = CliRunner()
    runner.invoke(cli, _base_args(cli_root) + ["db", "init"])
    result = runner.invoke(cli, _base_args(cli_root) + ["decide", "list"])
    assert result.exit_code == 0, result.output
    assert "no packets" in result.output


def test_decide_list_shows_pending(cli_root: Path) -> None:
    runner = CliRunner()
    runner.invoke(cli, _base_args(cli_root) + ["db", "init"])
    pid = _seed_packet(cli_root)
    result = runner.invoke(cli, _base_args(cli_root) + ["decide", "list"])
    assert result.exit_code == 0, result.output
    assert pid in result.output
    assert "US.NVDA" in result.output
    assert "rsi_oversold" in result.output
    assert "pending" in result.output


def test_decide_list_state_filter(cli_root: Path) -> None:
    runner = CliRunner()
    runner.invoke(cli, _base_args(cli_root) + ["db", "init"])
    _seed_packet(cli_root)
    result = runner.invoke(
        cli, _base_args(cli_root) + ["decide", "list", "--state", "executed"]
    )
    assert result.exit_code == 0
    assert "no packets in state=executed" in result.output


def test_decide_show_prints_markdown(cli_root: Path) -> None:
    runner = CliRunner()
    runner.invoke(cli, _base_args(cli_root) + ["db", "init"])
    pid = _seed_packet(cli_root)
    result = runner.invoke(cli, _base_args(cli_root) + ["decide", "show", pid])
    assert result.exit_code == 0, result.output
    assert "致 Claude" in result.output  # the self-dialogue header
    assert pid in result.output


def test_decide_show_unknown_id(cli_root: Path) -> None:
    runner = CliRunner()
    runner.invoke(cli, _base_args(cli_root) + ["db", "init"])
    result = runner.invoke(
        cli, _base_args(cli_root) + ["decide", "show", "no_such_packet"]
    )
    assert result.exit_code != 0
    assert "not found" in result.output


def test_decide_cancel_pending(cli_root: Path) -> None:
    runner = CliRunner()
    runner.invoke(cli, _base_args(cli_root) + ["db", "init"])
    pid = _seed_packet(cli_root)
    result = runner.invoke(cli, _base_args(cli_root) + ["decide", "cancel", pid])
    assert result.exit_code == 0, result.output
    # Confirm via list
    result2 = runner.invoke(
        cli, _base_args(cli_root) + ["decide", "list", "--state", "cancelled"]
    )
    assert pid in result2.output


# ---------------------------------------------------------------------------
# submit — the heart of HITL.
# ---------------------------------------------------------------------------


def test_decide_submit_buy_places_paper_trade(
    cli_root: Path, monkeypatch
) -> None:
    """Full happy path: paste decision → packet executed → DB Trade row."""
    runner = CliRunner()
    runner.invoke(cli, _base_args(cli_root) + ["db", "init"])
    runner.invoke(cli, _base_args(cli_root) + ["watchlist", "sync"])
    pid = _seed_packet(cli_root)

    fake = FakePaperTrader()
    fake.set_mark("US.NVDA", 850.0)
    monkeypatch.setattr("vibe_trader.cli.main._make_trader", lambda cfg: fake)

    decision = json.dumps(
        {
            "action": "BUY",
            "qty": 50,
            "confidence": 0.8,
            "reason": "RSI 超卖反弹机会",
            "memory_used": ["transcript: 调度官模式"],
        }
    )
    result = runner.invoke(
        cli,
        _base_args(cli_root) + ["decide", "submit", pid, "--json", decision],
    )
    assert result.exit_code == 0, result.output
    assert "paper trade placed" in result.output
    assert "BUY 50 US.NVDA" in result.output

    # Packet now in executed
    list_res = runner.invoke(
        cli, _base_args(cli_root) + ["decide", "list", "--state", "executed"]
    )
    assert pid in list_res.output

    # DB has the trade + position
    engine = make_engine(str(cli_root / "data" / "x.db"), wal_mode=False)
    factory = make_sessionmaker(engine)
    with session_scope(factory) as s:
        trades = s.query(Trade).all()
        assert len(trades) == 1
        assert trades[0].qty == 50
        positions = s.query(Position).filter(Position.qty > 0).all()
        assert len(positions) == 1
        assert positions[0].avg_cost == 850.0
        # The synthesised SignalRow used the packet id in its signal_type
        sig = s.query(SignalRow).filter(SignalRow.id == trades[0].signal_id).one()
        assert pid in sig.signal_type


def test_decide_submit_hold_records_no_trade(
    cli_root: Path, monkeypatch
) -> None:
    runner = CliRunner()
    runner.invoke(cli, _base_args(cli_root) + ["db", "init"])
    pid = _seed_packet(cli_root)

    fake = FakePaperTrader()
    fake.set_mark("US.NVDA", 850.0)
    monkeypatch.setattr("vibe_trader.cli.main._make_trader", lambda cfg: fake)

    decision = json.dumps(
        {
            "action": "HOLD",
            "qty": 0,
            "confidence": 0.5,
            "reason": "信号不足",
        }
    )
    result = runner.invoke(
        cli,
        _base_args(cli_root) + ["decide", "submit", pid, "--json", decision],
    )
    assert result.exit_code == 0, result.output
    assert "HOLD recorded" in result.output

    # No trade in DB
    engine = make_engine(str(cli_root / "data" / "x.db"), wal_mode=False)
    factory = make_sessionmaker(engine)
    with session_scope(factory) as s:
        assert s.query(Trade).count() == 0


def test_decide_submit_no_execute_flag(cli_root: Path, monkeypatch) -> None:
    """--no-execute records the decision but doesn't trade."""
    runner = CliRunner()
    runner.invoke(cli, _base_args(cli_root) + ["db", "init"])
    pid = _seed_packet(cli_root)

    monkeypatch.setattr(
        "vibe_trader.cli.main._make_trader",
        lambda cfg: pytest.fail("trader should NOT be built when --no-execute"),
    )

    decision = json.dumps(
        {"action": "BUY", "qty": 50, "confidence": 0.9, "reason": "x"}
    )
    result = runner.invoke(
        cli,
        _base_args(cli_root)
        + ["decide", "submit", pid, "--json", decision, "--no-execute"],
    )
    assert result.exit_code == 0, result.output
    assert "stopping before paper trade" in result.output

    # State is submitted (NOT executed): we recorded the decision but
    # didn't run the executor.
    submitted_res = runner.invoke(
        cli, _base_args(cli_root) + ["decide", "list", "--state", "submitted"]
    )
    assert pid in submitted_res.output


def test_decide_submit_rejects_invalid_json(cli_root: Path) -> None:
    runner = CliRunner()
    runner.invoke(cli, _base_args(cli_root) + ["db", "init"])
    pid = _seed_packet(cli_root)
    result = runner.invoke(
        cli,
        _base_args(cli_root) + ["decide", "submit", pid, "--json", "{not valid json}"],
    )
    assert result.exit_code != 0
    assert "invalid JSON" in result.output


def test_decide_submit_rejects_missing_required_fields(cli_root: Path) -> None:
    runner = CliRunner()
    runner.invoke(cli, _base_args(cli_root) + ["db", "init"])
    pid = _seed_packet(cli_root)
    result = runner.invoke(
        cli,
        _base_args(cli_root)
        + ["decide", "submit", pid, "--json", '{"action": "BUY"}'],
    )
    assert result.exit_code != 0
    # PacketStore raises ValueError("missing required fields ...")
    assert "missing required fields" in result.output


def test_decide_submit_rejects_unknown_packet(cli_root: Path) -> None:
    runner = CliRunner()
    runner.invoke(cli, _base_args(cli_root) + ["db", "init"])
    decision = json.dumps(
        {"action": "BUY", "qty": 1, "confidence": 0.9, "reason": "x"}
    )
    result = runner.invoke(
        cli,
        _base_args(cli_root)
        + ["decide", "submit", "no_such_packet", "--json", decision],
    )
    assert result.exit_code != 0
    assert "not found" in result.output


def test_decide_submit_low_confidence_demoted_to_hold(
    cli_root: Path, monkeypatch
) -> None:
    """When confidence < min_confidence, enforce_constraints demotes BUY → HOLD.

    The packet should land in executed (with status=HOLD) — not in
    submitted-pending-execution. The user gets clear feedback.
    """
    runner = CliRunner()
    runner.invoke(cli, _base_args(cli_root) + ["db", "init"])
    pid = _seed_packet(cli_root)

    fake = FakePaperTrader()
    fake.set_mark("US.NVDA", 850.0)
    monkeypatch.setattr("vibe_trader.cli.main._make_trader", lambda cfg: fake)

    decision = json.dumps(
        {
            "action": "BUY",
            "qty": 50,
            "confidence": 0.3,  # below 0.6 threshold
            "reason": "low conviction",
        }
    )
    result = runner.invoke(
        cli,
        _base_args(cli_root) + ["decide", "submit", pid, "--json", decision],
    )
    assert result.exit_code == 0, result.output
    assert "HOLD recorded" in result.output

    # No trade
    engine = make_engine(str(cli_root / "data" / "x.db"), wal_mode=False)
    factory = make_sessionmaker(engine)
    with session_scope(factory) as s:
        assert s.query(Trade).count() == 0


def test_decide_submit_already_submitted_packet_rejected(
    cli_root: Path, monkeypatch
) -> None:
    """Idempotency: re-submitting an already-submitted packet errors out."""
    runner = CliRunner()
    runner.invoke(cli, _base_args(cli_root) + ["db", "init"])
    runner.invoke(cli, _base_args(cli_root) + ["watchlist", "sync"])
    pid = _seed_packet(cli_root)

    fake = FakePaperTrader()
    fake.set_mark("US.NVDA", 850.0)
    monkeypatch.setattr("vibe_trader.cli.main._make_trader", lambda cfg: fake)

    decision = json.dumps(
        {"action": "BUY", "qty": 50, "confidence": 0.8, "reason": "x"}
    )
    runner.invoke(
        cli,
        _base_args(cli_root) + ["decide", "submit", pid, "--json", decision],
    )
    # Now packet is executed; submitting again must fail
    result = runner.invoke(
        cli,
        _base_args(cli_root) + ["decide", "submit", pid, "--json", decision],
    )
    assert result.exit_code != 0
    assert "executed" in result.output or "submitted" in result.output


# ---------------------------------------------------------------------------
# --file flag.
# ---------------------------------------------------------------------------


def test_decide_submit_from_file(cli_root: Path, monkeypatch) -> None:
    runner = CliRunner()
    runner.invoke(cli, _base_args(cli_root) + ["db", "init"])
    runner.invoke(cli, _base_args(cli_root) + ["watchlist", "sync"])
    pid = _seed_packet(cli_root)

    decision_file = cli_root / "decision.json"
    decision_file.write_text(
        json.dumps(
            {"action": "HOLD", "qty": 0, "confidence": 0.4, "reason": "等等看"}
        )
    )

    fake = FakePaperTrader()
    monkeypatch.setattr("vibe_trader.cli.main._make_trader", lambda cfg: fake)

    result = runner.invoke(
        cli,
        _base_args(cli_root)
        + ["decide", "submit", pid, "--file", str(decision_file)],
    )
    assert result.exit_code == 0, result.output


def test_decide_submit_requires_exactly_one_input(cli_root: Path) -> None:
    runner = CliRunner()
    runner.invoke(cli, _base_args(cli_root) + ["db", "init"])
    pid = _seed_packet(cli_root)
    # Neither --json nor --file
    result = runner.invoke(
        cli, _base_args(cli_root) + ["decide", "submit", pid]
    )
    assert result.exit_code != 0
    assert "exactly one" in result.output


# ---------------------------------------------------------------------------
# Help text — make sure the group is discoverable.
# ---------------------------------------------------------------------------


def test_decide_help_lists_subcommands() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["decide", "--help"])
    assert result.exit_code == 0
    for sub in ("list", "show", "submit", "cancel"):
        assert sub in result.output
