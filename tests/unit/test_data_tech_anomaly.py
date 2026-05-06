from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import patch

import pytest

from vibe_trader.data.tech_anomaly import _parse, fetch_tech_anomalies


def test_parse_extracts_anomalies() -> None:
    payload = {
        "anomalies": [
            {
                "code": "US.NVDA",
                "ts": "2026-05-02T14:30:00",
                "indicator": "MACD",
                "event": "death_cross",
                "description": "MACD 柱由正转负",
            }
        ]
    }
    out = _parse(payload)
    assert len(out) == 1
    assert out[0].event == "death_cross"
    assert out[0].ts == datetime(2026, 5, 2, 14, 30)


def test_parse_empty_anomalies_yields_empty_list() -> None:
    assert _parse({"anomalies": []}) == []
    assert _parse({}) == []


def test_fetch_invokes_subprocess_and_parses() -> None:
    fake_stdout = json.dumps(
        {
            "anomalies": [
                {
                    "code": "US.AAPL",
                    "ts": "2026-05-02T14:30:00",
                    "indicator": "RSI",
                    "event": "overbought",
                    "description": "RSI=72",
                }
            ]
        }
    )
    with patch("vibe_trader.data.tech_anomaly.subprocess.run") as run:
        run.return_value.returncode = 0
        run.return_value.stdout = fake_stdout
        run.return_value.stderr = ""
        out = fetch_tech_anomalies(["US.AAPL"], script_path="/fake/run.py")
    assert len(out) == 1
    assert out[0].indicator == "RSI"


def test_fetch_raises_on_nonzero_exit() -> None:
    with patch("vibe_trader.data.tech_anomaly.subprocess.run") as run:
        run.return_value.returncode = 1
        run.return_value.stdout = ""
        run.return_value.stderr = "OpenD not connected"
        with pytest.raises(RuntimeError, match="OpenD not connected"):
            fetch_tech_anomalies(["US.AAPL"], script_path="/fake/run.py")
