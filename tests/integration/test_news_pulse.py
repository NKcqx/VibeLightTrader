from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from sqlalchemy.orm import sessionmaker

from equity_monitor.config import AppConfig, WatchlistConfig
from equity_monitor.data.news import NewsItem
from equity_monitor.data.sentiment import SentimentSnapshot
from equity_monitor.db import session_scope
from equity_monitor.models import NewsDigest, Symbol
from equity_monitor.scheduler.jobs import run_news_pulse


def _news(code: str, url: str, title: str = "headline") -> NewsItem:
    return NewsItem(
        code=code,
        ts=datetime(2026, 5, 2, 14, 0),
        source="Reuters",
        title=title,
        url=url,
        summary="x",
    )


def _sent(code: str, temp: float) -> SentimentSnapshot:
    return SentimentSnapshot(
        code=code,
        ts=datetime(2026, 5, 2, 14, 30),
        temperature=temp,
        bullish_pct=20.0,
        bearish_pct=70.0,
        sample_size=400,
    )


@pytest.mark.integration
def test_news_pulse_negative_burst(
    factory: sessionmaker, app_cfg: AppConfig, watchlist: WatchlistConfig
) -> None:
    with session_scope(factory) as s:
        s.add(
            Symbol(
                code="US.AAPL",
                name="Apple",
                upper_threshold=200.0,
                lower_threshold=165.0,
            )
        )

    sent_cards: list[dict[str, Any]] = []

    def fake_sender(card, *_):  # type: ignore[no-untyped-def]
        sent_cards.append(card)
        return "om_test"

    history = {"US.AAPL": 7.0}
    out = run_news_pulse(
        factory=factory,
        cfg=app_cfg,
        watchlist=watchlist,
        fetch_news=lambda codes: [_news("US.AAPL", "https://r.com/1", "AAPL guidance miss")],
        fetch_sent=lambda codes: [_sent("US.AAPL", 3.5)],
        sentiment_history=history,
        send_card_fn=fake_sender,
    )

    assert out["pushed"] == 1
    assert out["news_inserted"] == 1
    assert sent_cards[0]["header"]["template"] == "red"
    assert history["US.AAPL"] == 3.5

    with session_scope(factory) as s:
        assert s.query(NewsDigest).count() == 1


@pytest.mark.integration
def test_news_pulse_positive_burst_green_card(
    factory: sessionmaker, app_cfg: AppConfig, watchlist: WatchlistConfig
) -> None:
    with session_scope(factory) as s:
        s.add(Symbol(code="US.AAPL", name="Apple"))

    sent: list[dict[str, Any]] = []

    out = run_news_pulse(
        factory=factory,
        cfg=app_cfg,
        watchlist=watchlist,
        fetch_news=lambda c: [_news("US.AAPL", "https://r.com/up", "AAPL beats")],
        fetch_sent=lambda c: [_sent("US.AAPL", 9.0)],
        sentiment_history={"US.AAPL": 5.0},
        send_card_fn=lambda card, *_: (sent.append(card) or "om_pos"),
    )
    assert out["pushed"] == 1
    assert sent[0]["header"]["template"] == "green"


@pytest.mark.integration
def test_news_pulse_no_baseline_no_push(
    factory: sessionmaker, app_cfg: AppConfig, watchlist: WatchlistConfig
) -> None:
    """First observation of a code seeds history and does NOT push."""
    with session_scope(factory) as s:
        s.add(Symbol(code="US.AAPL", name="Apple"))

    history: dict[str, float] = {}
    out = run_news_pulse(
        factory=factory,
        cfg=app_cfg,
        watchlist=watchlist,
        fetch_news=lambda c: [],
        fetch_sent=lambda c: [_sent("US.AAPL", 3.0)],
        sentiment_history=history,
        send_card_fn=lambda *args: "om_x",
    )
    assert out["pushed"] == 0
    assert history["US.AAPL"] == 3.0


@pytest.mark.integration
def test_news_pulse_below_threshold_no_push(
    factory: sessionmaker, app_cfg: AppConfig, watchlist: WatchlistConfig
) -> None:
    with session_scope(factory) as s:
        s.add(Symbol(code="US.AAPL", name="Apple"))

    out = run_news_pulse(
        factory=factory,
        cfg=app_cfg,
        watchlist=watchlist,
        fetch_news=lambda c: [],
        fetch_sent=lambda c: [_sent("US.AAPL", 6.0)],
        sentiment_history={"US.AAPL": 5.0},
        send_card_fn=lambda *args: "x",
    )
    assert out["pushed"] == 0


@pytest.mark.integration
def test_news_pulse_idempotent_on_url(
    factory: sessionmaker, app_cfg: AppConfig, watchlist: WatchlistConfig
) -> None:
    """Same news URL → second persist inserts 0 rows (ON CONFLICT DO NOTHING)."""
    with session_scope(factory) as s:
        s.add(Symbol(code="US.AAPL", name="Apple"))

    items = [_news("US.AAPL", "https://r.com/dup", "title-1")]
    history = {"US.AAPL": 5.0}

    out1 = run_news_pulse(
        factory=factory,
        cfg=app_cfg,
        watchlist=watchlist,
        fetch_news=lambda c: items,
        fetch_sent=lambda c: [_sent("US.AAPL", 5.1)],
        sentiment_history=history,
        send_card_fn=lambda *args: "x",
    )
    out2 = run_news_pulse(
        factory=factory,
        cfg=app_cfg,
        watchlist=watchlist,
        fetch_news=lambda c: items,
        fetch_sent=lambda c: [_sent("US.AAPL", 5.1)],
        sentiment_history=history,
        send_card_fn=lambda *args: "x",
    )
    assert out1["news_inserted"] == 1
    assert out2["news_inserted"] == 0
    with session_scope(factory) as s:
        assert s.query(NewsDigest).count() == 1
