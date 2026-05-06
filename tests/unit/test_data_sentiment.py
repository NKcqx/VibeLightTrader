from __future__ import annotations

from datetime import datetime

from vibe_trader.data.sentiment import _parse


def test_parse_sentiment() -> None:
    payload = {
        "snapshots": [
            {
                "code": "US.AAPL",
                "ts": "2026-05-02T14:30:00",
                "temperature": 7.2,
                "bullish_pct": 62.5,
                "bearish_pct": 18.0,
                "sample_size": 480,
            }
        ]
    }
    out = _parse(payload)
    assert out[0].temperature == 7.2
    assert out[0].bullish_pct == 62.5
    assert out[0].bearish_pct == 18.0
    assert out[0].sample_size == 480
    assert out[0].ts == datetime(2026, 5, 2, 14, 30)


def test_parse_missing_optional_fields_default() -> None:
    payload = {
        "snapshots": [
            {
                "code": "US.NVDA",
                "ts": "2026-05-02T14:30:00",
                "temperature": 5.0,
            }
        ]
    }
    out = _parse(payload)
    assert out[0].bullish_pct == 0.0
    assert out[0].bearish_pct == 0.0
    assert out[0].sample_size == 0
