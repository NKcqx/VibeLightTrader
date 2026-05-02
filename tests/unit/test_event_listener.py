from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import sessionmaker

from equity_monitor.db import init_schema, make_engine, make_sessionmaker, session_scope
from equity_monitor.events.listener import _extract_text, dispatch_event
from equity_monitor.models import Symbol


@pytest.fixture
def factory(tmp_path: Path) -> sessionmaker:
    engine = make_engine(str(tmp_path / "x.db"), wal_mode=False)
    init_schema(engine)
    return make_sessionmaker(engine)


def _evt(text: str, open_id: str = "ou_user1") -> dict[str, Any]:
    return {
        "event_type": "im.message.receive_v1",
        "event": {
            "sender": {"sender_id": {"open_id": open_id}},
            "message": {
                "message_id": "om_test",
                "chat_id": "oc_test",
                "chat_type": "p2p",
                "message_type": "text",
                "content": json.dumps({"text": text}),
            },
        },
    }


def test_extract_text_returns_text_and_sender() -> None:
    text, sender = _extract_text(_evt("/list", open_id="ou_alice"))
    assert text == "/list"
    assert sender == "ou_alice"


def test_extract_text_returns_none_for_other_event_types() -> None:
    text, sender = _extract_text({"event_type": "contact.user.updated_v3"})
    assert text is None and sender is None


def test_extract_text_returns_none_for_non_text_messages() -> None:
    e = _evt("hello")
    e["event"]["message"]["message_type"] = "image"
    text, sender = _extract_text(e)
    assert text is None and sender is None


def test_dispatch_unrecognized_text_no_reply(factory: sessionmaker) -> None:
    sent: list[tuple[str, str]] = []
    out = dispatch_event(
        _evt("hi how are you"),
        factory=factory,
        allowed_open_id="ou_user1",
        send_text=lambda t, r: (sent.append((t, r)), "om_x")[1],
    )
    assert out is None
    assert sent == []


def test_dispatch_list_command_replies(factory: sessionmaker) -> None:
    sent: list[tuple[str, str]] = []
    out = dispatch_event(
        _evt("/list"),
        factory=factory,
        allowed_open_id="ou_user1",
        send_text=lambda t, r: (sent.append((t, r)), "om_x")[1],
    )
    assert out is not None
    assert "监控列表为空" in out
    assert sent == [(out, "ou_user1")]


def test_dispatch_add_command_persists(factory: sessionmaker) -> None:
    sent: list[tuple[str, str]] = []
    dispatch_event(
        _evt("添加 US.AAPL 上限200 下限165"),
        factory=factory,
        allowed_open_id="ou_user1",
        send_text=lambda t, r: (sent.append((t, r)), "om_x")[1],
    )
    with session_scope(factory) as s:
        row = s.query(Symbol).filter(Symbol.code == "US.AAPL").one()
        assert row.upper_threshold == 200.0
        assert row.lower_threshold == 165.0
    assert "已添加" in sent[0][0]


def test_dispatch_unauthorized_sender_ignored(factory: sessionmaker) -> None:
    sent: list[tuple[str, str]] = []
    out = dispatch_event(
        _evt("/list", open_id="ou_attacker"),
        factory=factory,
        allowed_open_id="ou_user1",
        send_text=lambda t, r: (sent.append((t, r)), "om_x")[1],
    )
    assert out is None
    assert sent == []


def test_dispatch_no_allowlist_accepts_anyone(factory: sessionmaker) -> None:
    """When allowed_open_id=None, any sender is allowed."""
    sent: list[tuple[str, str]] = []
    out = dispatch_event(
        _evt("/list", open_id="ou_anyone"),
        factory=factory,
        allowed_open_id=None,
        send_text=lambda t, r: (sent.append((t, r)), "om_x")[1],
    )
    assert out is not None
    assert sent[0][1] == "ou_anyone"


def test_dispatch_apply_exception_replies_with_error(factory: sessionmaker) -> None:
    """If apply() blows up, listener still replies with a graceful error message."""
    sent: list[tuple[str, str]] = []

    # Use an invalid command that bypasses parse but explodes on apply
    # (Easiest: monkeypatch apply via a custom factory that's broken)
    broken_factory = "not a sessionmaker"  # type: ignore[assignment]
    out = dispatch_event(
        _evt("/list"),
        factory=broken_factory,  # type: ignore[arg-type]
        allowed_open_id="ou_user1",
        send_text=lambda t, r: (sent.append((t, r)), "om_x")[1],
    )
    assert out is not None
    assert "处理失败" in out
    assert sent[0][1] == "ou_user1"


def test_dispatch_sender_failure_returns_none(factory: sessionmaker) -> None:
    """If reply send fails, dispatch_event swallows and returns None (no crash)."""

    def boom(t: str, r: str) -> str:
        raise RuntimeError("network down")

    out = dispatch_event(
        _evt("/list"),
        factory=factory,
        allowed_open_id="ou_user1",
        send_text=boom,
    )
    assert out is None


def test_run_listener_processes_injected_events(factory: sessionmaker) -> None:
    """End-to-end via injected event iterable (no subprocess)."""
    from equity_monitor.config import (
        AppConfig,
        DatabaseConfig,
        LarkConfig,
        LarkReceiver,
        LoggingConfig,
        OpenDConfig,
        SchedulerConfig,
        SignalsConfig,
    )
    from equity_monitor.events.listener import run_listener

    cfg = AppConfig(
        opend=OpenDConfig(host="127.0.0.1", port=11111),
        database=DatabaseConfig(path=":memory:", wal_mode=False),
        scheduler=SchedulerConfig(timezone="UTC", jobs={}),
        lark=LarkConfig(
            cli_path="lark-cli",
            identity="bot",
            receiver=LarkReceiver(type="user", open_id="ou_user1"),
        ),
        signals=SignalsConfig(),
        logging=LoggingConfig(),
    )

    sent: list[tuple[str, str]] = []
    # Monkey-patch make_text_sender to bypass real lark-cli
    import equity_monitor.events.listener as listener_mod

    listener_mod.make_text_sender = lambda **kw: (
        lambda t, r: (sent.append((t, r)), "om_x")[1]
    )  # type: ignore[assignment]

    events = [
        _evt("添加 US.AAPL 上限200 下限165"),
        _evt("/list"),
    ]
    run_listener(cfg=cfg, factory=factory, events=iter(events))

    assert len(sent) == 2
    assert "已添加" in sent[0][0]
    assert "US.AAPL" in sent[1][0]
    with session_scope(factory) as s:
        assert s.query(Symbol).filter(Symbol.code == "US.AAPL").count() == 1
