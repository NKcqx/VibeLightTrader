from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from tenacity import wait_none

from equity_monitor.reports import lark as lark_mod
from equity_monitor.reports.lark import LarkSendError, send_card


@pytest.fixture(autouse=True)
def _no_retry_wait(monkeypatch):
    """Make tenacity not actually sleep between retries — tests stay fast."""
    monkeypatch.setattr(send_card.retry, "wait", wait_none())
    yield


def test_send_card_success_returns_message_id() -> None:
    with patch.object(lark_mod, "subprocess") as sp:
        sp.run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"message_id": "om_xxx"}),
            stderr="",
        )
        msg_id = send_card({"foo": "bar"}, open_id="ou_aaa", cli_path="lark-cli")
    assert msg_id == "om_xxx"


def test_send_card_success_returns_raw_when_not_json() -> None:
    with patch.object(lark_mod, "subprocess") as sp:
        sp.run.return_value = MagicMock(
            returncode=0,
            stdout="raw-msg-id-xyz",
            stderr="",
        )
        msg_id = send_card({"foo": "bar"}, open_id="ou_aaa")
    assert msg_id == "raw-msg-id-xyz"


def test_send_card_failure_raises_after_retries() -> None:
    with patch.object(lark_mod, "subprocess") as sp:
        sp.run.return_value = MagicMock(returncode=1, stdout="", stderr="auth fail")
        with pytest.raises(LarkSendError, match="auth fail"):
            send_card({"foo": "bar"}, open_id="ou_aaa")
        assert sp.run.call_count == 3


def test_send_card_command_format() -> None:
    """Verify the lark-cli command is constructed as documented."""
    with patch.object(lark_mod, "subprocess") as sp:
        sp.run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        send_card(
            {"hello": "world"},
            open_id="ou_zzz",
            receiver_type="user",
            cli_path="/usr/bin/lark-cli",
        )
    cmd = sp.run.call_args[0][0]
    assert cmd[0] == "/usr/bin/lark-cli"
    assert cmd[1:3] == ["im", "+send-card"]
    assert "--user-open-id" in cmd
    assert "ou_zzz" in cmd
    assert "--card" in cmd
    payload_idx = cmd.index("--card") + 1
    assert json.loads(cmd[payload_idx]) == {"hello": "world"}
