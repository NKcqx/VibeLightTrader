from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from vibe_trader.cli.main import cli


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
                    "receiver": {"type": "chat", "open_id": "ou_test"},
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
                    },
                    {"code": "US.NVDA", "name": "NVIDIA"},
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


def test_help_shows_all_groups() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0, result.output
    for cmd in ("run", "once", "watchlist", "db"):
        assert cmd in result.output


def test_db_init_creates_file(cli_root: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, _base_args(cli_root) + ["db", "init"])
    assert result.exit_code == 0, result.output
    assert (cli_root / "data" / "x.db").exists()
    assert "initialized" in result.output


def test_watchlist_list_empty_until_sync(cli_root: Path) -> None:
    runner = CliRunner()
    runner.invoke(cli, _base_args(cli_root) + ["db", "init"])
    result = runner.invoke(cli, _base_args(cli_root) + ["watchlist", "list"])
    assert result.exit_code == 0, result.output
    assert "no active symbols" in result.output


def test_watchlist_sync_then_list(cli_root: Path) -> None:
    runner = CliRunner()
    runner.invoke(cli, _base_args(cli_root) + ["db", "init"])
    r1 = runner.invoke(cli, _base_args(cli_root) + ["watchlist", "sync"])
    assert r1.exit_code == 0, r1.output
    assert "synced 2 symbols" in r1.output
    r2 = runner.invoke(cli, _base_args(cli_root) + ["watchlist", "list"])
    assert r2.exit_code == 0, r2.output
    assert "US.AAPL" in r2.output
    assert "US.NVDA" in r2.output


def test_watchlist_sync_is_idempotent_and_updates(cli_root: Path) -> None:
    """Re-running sync must not duplicate rows; updates fields in-place."""
    runner = CliRunner()
    runner.invoke(cli, _base_args(cli_root) + ["db", "init"])
    runner.invoke(cli, _base_args(cli_root) + ["watchlist", "sync"])

    new_yaml = {
        "symbols": [
            {
                "code": "US.AAPL",
                "name": "Apple Inc.",
                "upper_threshold": 250,
                "lower_threshold": 150,
            }
        ]
    }
    (cli_root / "config" / "watchlist.yaml").write_text(yaml.safe_dump(new_yaml))

    r2 = runner.invoke(cli, _base_args(cli_root) + ["watchlist", "sync"])
    assert r2.exit_code == 0
    out = runner.invoke(cli, _base_args(cli_root) + ["watchlist", "list"]).output
    assert "Apple Inc." in out
    assert "upper=250" in out
    assert "lower=150" in out


def test_db_status_prints_zero_counts(cli_root: Path) -> None:
    runner = CliRunner()
    runner.invoke(cli, _base_args(cli_root) + ["db", "init"])
    result = runner.invoke(cli, _base_args(cli_root) + ["db", "status"])
    assert result.exit_code == 0, result.output
    assert "symbols:" in result.output
    assert "quotes:" in result.output
    assert "sentiment_snapshots:" in result.output


def test_once_news_skips_opend(cli_root: Path) -> None:
    """`once --job news` MUST NOT instantiate OpenDClient (the news job has no
    quote/kline dependency). We verify by patching `run_news_pulse` itself."""
    runner = CliRunner()
    runner.invoke(cli, _base_args(cli_root) + ["db", "init"])

    from unittest.mock import patch

    with (
        patch(
            "vibe_trader.cli.main.run_news_pulse",
            return_value={"news_inserted": 0, "pushed": 0},
        ) as mock_news,
        patch("vibe_trader.cli.main.OpenDClient") as mock_opend,
    ):
        result = runner.invoke(
            cli, _base_args(cli_root) + ["once", "--job", "news"]
        )
    assert result.exit_code == 0, result.output
    assert "news_inserted" in result.output
    mock_news.assert_called_once()
    mock_opend.assert_not_called()


def test_once_unknown_job_rejected(cli_root: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli, _base_args(cli_root) + ["once", "--job", "lunch"]
    )
    assert result.exit_code != 0
    assert "Invalid value" in result.output or "invalid choice" in result.output
