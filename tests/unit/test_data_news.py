from __future__ import annotations

from datetime import datetime

from equity_monitor.data.news import _parse


def test_parse_news_by_code() -> None:
    payload = {
        "by_code": {
            "US.AAPL": [
                {
                    "ts": "2026-05-02T13:00:00",
                    "source": "Reuters",
                    "title": "AAPL beats Q3 expectations",
                    "url": "https://reuters.com/x",
                    "summary": "Strong iPhone sales drive earnings",
                }
            ]
        }
    }
    out = _parse(payload)
    assert len(out) == 1
    assert out[0].code == "US.AAPL"
    assert out[0].source == "Reuters"
    assert out[0].ts == datetime(2026, 5, 2, 13, 0)


def test_parse_multiple_codes_and_items() -> None:
    payload = {
        "by_code": {
            "US.AAPL": [
                {
                    "ts": "2026-05-02T13:00:00",
                    "title": "iPhone 18 leak",
                    "url": "https://example.com/a",
                },
                {
                    "ts": "2026-05-02T14:00:00",
                    "title": "Vision Pro 2 rumors",
                    "url": "https://example.com/b",
                },
            ],
            "US.NVDA": [
                {
                    "ts": "2026-05-02T13:30:00",
                    "title": "Blackwell shipping",
                    "url": "https://example.com/c",
                }
            ],
        }
    }
    out = _parse(payload)
    assert len(out) == 3
    codes = {item.code for item in out}
    assert codes == {"US.AAPL", "US.NVDA"}


def test_parse_optional_fields_default_to_none() -> None:
    payload = {
        "by_code": {
            "US.AAPL": [
                {
                    "ts": "2026-05-02T13:00:00",
                    "title": "Anonymous post",
                    "url": "https://example.com/x",
                }
            ]
        }
    }
    out = _parse(payload)
    assert out[0].source is None
    assert out[0].summary is None
