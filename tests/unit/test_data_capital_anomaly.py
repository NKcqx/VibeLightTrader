from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import patch

import pytest

from equity_monitor.data.capital_anomaly import _parse, fetch_capital_anomalies


def test_parse_capital_anomaly() -> None:
    payload = {
        "anomalies": [
            {
                "code": "US.NVDA",
                "ts": "2026-05-02T14:30:00",
                "flow_type": "main_outflow",
                "amount": -12_400_000.0,
                "description": "主力净流出 12.4M",
            }
        ]
    }
    out = _parse(payload)
    assert len(out) == 1
    assert out[0].flow_type == "main_outflow"
    assert out[0].amount == -12_400_000.0
    assert out[0].ts == datetime(2026, 5, 2, 14, 30)


def test_parse_amount_defaults_to_zero() -> None:
    payload = {
        "anomalies": [
            {
                "code": "US.AAPL",
                "ts": "2026-05-02T14:30:00",
                "flow_type": "block_buy",
            }
        ]
    }
    out = _parse(payload)
    assert out[0].amount == 0.0
    assert out[0].description == ""


def test_fetch_subprocess_failure_raises() -> None:
    with patch("equity_monitor.data.capital_anomaly.subprocess.run") as run:
        run.return_value.returncode = 2
        run.return_value.stderr = "rate limited"
        with pytest.raises(RuntimeError, match="rate limited"):
            fetch_capital_anomalies(["US.AAPL"], script_path="/fake/run.py")


def test_fetch_subprocess_success() -> None:
    fake_stdout = json.dumps(
        {
            "anomalies": [
                {
                    "code": "US.TSLA",
                    "ts": "2026-05-02T15:00:00",
                    "flow_type": "main_inflow",
                    "amount": 8_500_000.0,
                    "description": "主力净流入 8.5M",
                }
            ]
        }
    )
    with patch("equity_monitor.data.capital_anomaly.subprocess.run") as run:
        run.return_value.returncode = 0
        run.return_value.stdout = fake_stdout
        out = fetch_capital_anomalies(["US.TSLA"], script_path="/fake/run.py")
    assert len(out) == 1
    assert out[0].flow_type == "main_inflow"
