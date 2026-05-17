"""Unit tests for the HTTP-backed reports.lark facade.

Verifies the thin wrapper around :class:`vibe_trader.lark.LarkHTTPClient`:
contract preservation (LarkSendError, retry policy, receiver_type mapping).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from tenacity import wait_none

from vibe_trader.lark.errors import LarkAPIError
from vibe_trader.reports import lark as lark_mod
from vibe_trader.reports.lark import LarkSendError, send_card


@pytest.fixture(autouse=True)
def _no_retry_wait(monkeypatch):
    """Make tenacity not actually sleep between retries — keeps tests fast."""
    monkeypatch.setattr(send_card.retry, "wait", wait_none())
    yield


def _client_returning(msg_id: str = "om_xxx") -> MagicMock:
    c = MagicMock()
    c.send_card.return_value = msg_id
    return c


def test_send_card_returns_message_id() -> None:
    client = _client_returning("om_xxx")
    msg_id = send_card({"foo": "bar"}, open_id="ou_aaa", client=client)
    assert msg_id == "om_xxx"
    client.send_card.assert_called_once()


def test_send_card_user_maps_to_open_id_receive_type() -> None:
    client = _client_returning()
    send_card({"hello": "world"}, open_id="ou_zzz", receiver_type="user", client=client)
    call = client.send_card.call_args
    assert call.args[0] == {"hello": "world"}
    assert call.kwargs == {"receive_id": "ou_zzz", "receive_id_type": "open_id"}


def test_send_card_chat_maps_to_chat_id_receive_type() -> None:
    client = _client_returning()
    send_card({"x": 1}, open_id="oc_chat_aaa", receiver_type="chat", client=client)
    call = client.send_card.call_args
    assert call.kwargs == {"receive_id": "oc_chat_aaa", "receive_id_type": "chat_id"}


def test_send_card_unknown_receiver_type_rejected() -> None:
    client = MagicMock()
    with pytest.raises(LarkSendError, match="unknown receiver_type"):
        send_card({}, open_id="x", receiver_type="weird", client=client)  # type: ignore[arg-type]


def test_send_card_wraps_lark_api_error_into_send_error() -> None:
    client = MagicMock()
    client.send_card.side_effect = LarkAPIError(230020, "no permission")
    with pytest.raises(LarkSendError, match="no permission"):
        send_card({}, open_id="ou_a", client=client)
    assert client.send_card.call_count == 3, "tenacity should retry up to 3 times"


def test_send_card_retries_then_succeeds() -> None:
    client = MagicMock()
    client.send_card.side_effect = [
        LarkAPIError(-1, "transient"),
        LarkAPIError(-1, "transient again"),
        "om_third_try",
    ]
    msg_id = send_card({}, open_id="ou_a", client=client)
    assert msg_id == "om_third_try"
    assert client.send_card.call_count == 3


def test_lark_send_error_module_attribute_preserved() -> None:
    """Old callers `import LarkSendError` from this module — keep the name."""
    assert hasattr(lark_mod, "LarkSendError")
    assert issubclass(lark_mod.LarkSendError, RuntimeError)
