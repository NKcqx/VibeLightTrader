from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from tenacity import wait_none

from vibe_trader.reports import lark as lark_mod
from vibe_trader.reports.lark import LarkSendError, send_card


@pytest.fixture(autouse=True)
def _no_retry_wait(monkeypatch):
    """Make tenacity not actually sleep between retries — tests stay fast."""
    monkeypatch.setattr(send_card.retry, "wait", wait_none())
    yield


def _ok_response(message_id: str = "om_xxx") -> str:
    return json.dumps(
        {
            "ok": True,
            "identity": "bot",
            "data": {
                "chat_id": "oc_test",
                "create_time": "2026-05-02 20:00:00",
                "message_id": message_id,
            },
        }
    )


def _err_response(typ: str, msg: str) -> str:
    return json.dumps({"ok": False, "error": {"type": typ, "message": msg}})


def test_send_card_success_returns_message_id() -> None:
    with patch.object(lark_mod, "subprocess") as sp:
        sp.run.return_value = MagicMock(
            returncode=0, stdout=_ok_response("om_xxx"), stderr=""
        )
        msg_id = send_card({"foo": "bar"}, open_id="ou_aaa", cli_path="lark-cli")
    assert msg_id == "om_xxx"


def test_send_card_lark_error_raises() -> None:
    """When lark-cli returns ok=false, raise LarkSendError carrying the type+message."""
    with patch.object(lark_mod, "subprocess") as sp:
        sp.run.return_value = MagicMock(
            returncode=3, stdout=_err_response("missing_scope", "need scope X"), stderr=""
        )
        with pytest.raises(LarkSendError, match="missing_scope|need scope X"):
            send_card({"foo": "bar"}, open_id="ou_aaa")
        assert sp.run.call_count == 3


def test_send_card_subprocess_failure_raises_after_retries() -> None:
    with patch.object(lark_mod, "subprocess") as sp:
        sp.run.return_value = MagicMock(returncode=1, stdout="", stderr="auth fail")
        with pytest.raises(LarkSendError, match="auth fail"):
            send_card({"foo": "bar"}, open_id="ou_aaa")
        assert sp.run.call_count == 3


def test_send_card_non_json_response_raises() -> None:
    with patch.object(lark_mod, "subprocess") as sp:
        sp.run.return_value = MagicMock(returncode=0, stdout="not json", stderr="")
        with pytest.raises(LarkSendError, match="non-JSON"):
            send_card({"foo": "bar"}, open_id="ou_aaa")


def test_send_card_user_command_format() -> None:
    """receiver_type=user → --user-id flag with bot identity."""
    with patch.object(lark_mod, "subprocess") as sp:
        sp.run.return_value = MagicMock(returncode=0, stdout=_ok_response(), stderr="")
        send_card(
            {"hello": "world"},
            open_id="ou_zzz",
            receiver_type="user",
            cli_path="/usr/bin/lark-cli",
        )
    cmd = sp.run.call_args[0][0]
    assert cmd[0] == "/usr/bin/lark-cli"
    assert cmd[1:3] == ["im", "+messages-send"]
    assert "--as" in cmd and cmd[cmd.index("--as") + 1] == "bot"
    assert "--user-id" in cmd and cmd[cmd.index("--user-id") + 1] == "ou_zzz"
    assert "--msg-type" in cmd and cmd[cmd.index("--msg-type") + 1] == "interactive"
    payload_idx = cmd.index("--content") + 1
    assert json.loads(cmd[payload_idx]) == {"hello": "world"}


def test_send_card_chat_uses_chat_id_flag() -> None:
    """receiver_type=chat → --chat-id flag (not --user-id)."""
    with patch.object(lark_mod, "subprocess") as sp:
        sp.run.return_value = MagicMock(returncode=0, stdout=_ok_response(), stderr="")
        send_card(
            {"x": 1},
            open_id="oc_chat_aaa",
            receiver_type="chat",
        )
    cmd = sp.run.call_args[0][0]
    assert "--chat-id" in cmd and cmd[cmd.index("--chat-id") + 1] == "oc_chat_aaa"
    assert "--user-id" not in cmd


def test_send_card_unknown_receiver_type_rejected() -> None:
    with pytest.raises(LarkSendError, match="unknown receiver_type"):
        send_card({}, open_id="x", receiver_type="weird")  # type: ignore[arg-type]


def test_send_card_user_identity_passed_through() -> None:
    """identity=user → --as user (e.g. for Phase 2 reply-from-account scenarios)."""
    with patch.object(lark_mod, "subprocess") as sp:
        sp.run.return_value = MagicMock(returncode=0, stdout=_ok_response(), stderr="")
        send_card({}, open_id="ou_aaa", identity="user")
    cmd = sp.run.call_args[0][0]
    assert cmd[cmd.index("--as") + 1] == "user"
