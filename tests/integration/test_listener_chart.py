from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from equity_monitor.events.listener import dispatch_event
from equity_monitor.futu_client import Candle, FakeFutuClient, Snapshot
from equity_monitor.models import Symbol


def _seed_kline(client: FakeFutuClient) -> None:
    candles = []
    base = datetime(2026, 5, 1, 9, 30, tzinfo=timezone.utc)
    for i in range(60):
        p = 100.0 + i
        candles.append(
            Candle(
                code="US.AAPL",
                ts=base + timedelta(hours=i),
                open=p,
                high=p + 1,
                low=p - 1,
                close=p + 0.5,
                volume=10_000,
                turnover=p * 10_000,
            )
        )
    client.set_kline("US.AAPL", "K_60M", candles)


def _make_event(text: str, sender: str = "ou_caller") -> dict:
    return {
        "event_type": "im.message.receive_v1",
        "event": {
            "sender": {"sender_id": {"open_id": sender}},
            "message": {
                "message_id": "om_x",
                "chat_type": "p2p",
                "message_type": "text",
                "content": json.dumps({"text": text}),
            },
        },
    }


@pytest.fixture()
def fake_client() -> FakeFutuClient:
    c = FakeFutuClient()
    _seed_kline(c)
    c.set_snapshot(
        Snapshot(
            code="US.AAPL",
            last_price=160.0,
            open_price=159.0,
            high_price=162.0,
            low_price=158.0,
            volume=100_000,
            turnover=16e6,
            update_time=datetime(2026, 5, 1, 16, 0, tzinfo=timezone.utc),
        )
    )
    return c


def test_chart_command_sends_text_and_image(
    fake_client: FakeFutuClient,
    factory: sessionmaker,
    tmp_path: Path,
) -> None:
    with factory() as s:
        s.add(Symbol(code="US.AAPL", name="Apple"))
        s.commit()

    sent_text: list[str] = []
    sent_images: list[Path] = []

    def fake_send_text(text: str, recipient: str) -> str:
        sent_text.append(text)
        return "om_text"

    def fake_send_image(path: Path, recipient: str) -> str:
        sent_images.append(path)
        return "om_img"

    res = dispatch_event(
        _make_event("/chart AAPL"),
        factory=factory,
        allowed_open_id="ou_caller",
        send_text=fake_send_text,
        client=fake_client,
        send_image=fake_send_image,
        snapshot_dir=tmp_path,
    )

    assert res is not None
    assert sent_text, "should have sent a text confirmation"
    assert "AAPL" in sent_text[0]
    assert sent_images, "should have sent an image"
    assert sent_images[0].exists()
    assert sent_images[0].suffix == ".png"


def test_chart_command_falls_back_when_client_missing(
    factory: sessionmaker, tmp_path: Path
) -> None:
    sent_text: list[str] = []

    def fake_send_text(text: str, recipient: str) -> str:
        sent_text.append(text)
        return "om_text"

    res = dispatch_event(
        _make_event("/chart AAPL"),
        factory=factory,
        allowed_open_id="ou_caller",
        send_text=fake_send_text,
        client=None,
        send_image=None,
        snapshot_dir=None,
    )
    assert res is not None
    assert sent_text and "/chart" in sent_text[0]
    assert "不可用" in sent_text[0]


def test_chart_with_unknown_freq_returns_none(
    fake_client: FakeFutuClient, factory: sessionmaker
) -> None:
    """grammar rejects bad freq → parse returns None → dispatch_event returns None silently."""
    sent_text: list[str] = []

    res = dispatch_event(
        _make_event("/chart AAPL bogus"),
        factory=factory,
        allowed_open_id="ou_caller",
        send_text=lambda text, r: (sent_text.append(text), "om")[1],
        client=fake_client,
    )
    assert res is None
    assert not sent_text  # silent


def test_chart_apply_failure_sends_error_text(
    factory: sessionmaker, tmp_path: Path
) -> None:
    """If apply_chart raises, the listener sends an error text via send_text."""

    class AngryClient:
        def snapshot(self, codes):
            return []

        def kline(self, code, *, ktype, limit):
            raise RuntimeError("OpenD blew up")

        def close(self):
            pass

    sent_text: list[str] = []

    def fake_send_text(text: str, recipient: str) -> str:
        sent_text.append(text)
        return "om_text"

    res = dispatch_event(
        _make_event("/chart AAPL"),
        factory=factory,
        allowed_open_id="ou_caller",
        send_text=fake_send_text,
        client=AngryClient(),
        send_image=lambda p, r: "om_img",
        snapshot_dir=tmp_path,
    )
    assert res is not None
    assert sent_text, "error text should still be sent"
    assert "/chart 失败" in sent_text[0]
    # No PNG should exist
    assert not list(tmp_path.glob("*.png"))


def test_chart_image_send_failure_does_not_block_text(
    fake_client: FakeFutuClient,
    factory: sessionmaker,
    tmp_path: Path,
) -> None:
    """If send_image raises, the caption text is still considered delivered."""
    with factory() as s:
        s.add(Symbol(code="US.AAPL", name="Apple"))
        s.commit()

    sent_text: list[str] = []

    def fake_send_text(text: str, recipient: str) -> str:
        sent_text.append(text)
        return "om_text"

    def angry_send_image(path: Path, recipient: str) -> str:
        raise RuntimeError("lark-cli image send failed")

    res = dispatch_event(
        _make_event("/chart AAPL"),
        factory=factory,
        allowed_open_id="ou_caller",
        send_text=fake_send_text,
        client=fake_client,
        send_image=angry_send_image,
        snapshot_dir=tmp_path,
    )

    assert res is not None
    assert sent_text and "AAPL" in sent_text[0]  # caption already delivered
