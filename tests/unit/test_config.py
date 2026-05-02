from __future__ import annotations

from pathlib import Path

import pytest

from equity_monitor.config import (
    AppConfig,
    SymbolConfig,
    load_settings,
    load_watchlist,
)


def test_load_watchlist_example(tmp_path: Path) -> None:
    yml = tmp_path / "watchlist.yaml"
    yml.write_text(
        """\
symbols:
  - code: US.AAPL
    name: Apple
    upper_threshold: 200.0
    lower_threshold: 165.0
"""
    )
    wl = load_watchlist(yml)
    assert len(wl.symbols) == 1
    s: SymbolConfig = wl.symbols[0]
    assert s.code == "US.AAPL"
    assert s.upper_threshold == 200.0


def test_load_settings_full(tmp_path: Path) -> None:
    yml = tmp_path / "settings.yaml"
    yml.write_text(Path("config/settings.yaml").read_text())
    cfg: AppConfig = load_settings(yml)
    assert cfg.opend.host == "127.0.0.1"
    assert cfg.opend.port == 11111
    assert cfg.scheduler.timezone == "America/New_York"
    assert cfg.signals.rsi_overbought == 70
    assert "intraday_check" in cfg.scheduler.jobs


def test_invalid_threshold_rejected(tmp_path: Path) -> None:
    yml = tmp_path / "bad.yaml"
    yml.write_text(
        """\
symbols:
  - code: US.AAPL
    name: Apple
    upper_threshold: -5.0
"""
    )
    with pytest.raises(ValueError):
        load_watchlist(yml)
