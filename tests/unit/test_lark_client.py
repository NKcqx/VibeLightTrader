from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from vibe_trader.lark.client import LarkHTTPClient
from vibe_trader.lark.errors import LarkAPIError


class _FakeTM:
    """Minimal TokenManager double — returns a fixed token, counts invalidates."""

    def __init__(self, token: str = "t_test") -> None:
        self.token = token
        self.invalidate_count = 0

    def get(self, *, force_refresh: bool = False) -> str:
        return self.token

    def invalidate(self) -> None:
        self.invalidate_count += 1


def _resp(status: int = 200, body: dict | None = None, *, raw_text: str = "") -> MagicMock:
    m = MagicMock()
    m.status_code = status
    if body is not None:
        m.json.return_value = body
        m.text = json.dumps(body)
    else:
        m.json.side_effect = ValueError("not json")
        m.text = raw_text
    return m


def _ok_send_resp(message_id: str = "om_test", chat_id: str = "oc_test") -> MagicMock:
    return _resp(
        200,
        {
            "code": 0,
            "msg": "success",
            "data": {"message_id": message_id, "chat_id": chat_id},
        },
    )


def _build_client(http: MagicMock) -> LarkHTTPClient:
    return LarkHTTPClient(_FakeTM(), base_url="https://open.feishu.cn", http_client=http)


# ----------------------------------------------------------------------
# send_card
# ----------------------------------------------------------------------


def test_send_card_posts_to_correct_url_and_returns_message_id() -> None:
    http = MagicMock()
    http.request.return_value = _ok_send_resp("om_card_1")
    client = _build_client(http)

    msg_id = client.send_card({"hello": "world"}, receive_id="ou_zzz")

    assert msg_id == "om_card_1"
    args, kwargs = http.request.call_args
    assert args[0] == "POST"
    assert args[1] == "https://open.feishu.cn/open-apis/im/v1/messages"
    assert kwargs["params"] == {"receive_id_type": "open_id"}
    body = kwargs["json"]
    assert body["receive_id"] == "ou_zzz"
    assert body["msg_type"] == "interactive"
    assert json.loads(body["content"]) == {"hello": "world"}


def test_send_card_chat_uses_chat_id_param() -> None:
    http = MagicMock()
    http.request.return_value = _ok_send_resp()
    client = _build_client(http)

    client.send_card({"x": 1}, receive_id="oc_chat", receive_id_type="chat_id")

    _, kwargs = http.request.call_args
    assert kwargs["params"] == {"receive_id_type": "chat_id"}
    assert kwargs["json"]["receive_id"] == "oc_chat"


def test_send_card_attaches_bearer_auth_header() -> None:
    http = MagicMock()
    http.request.return_value = _ok_send_resp()
    client = _build_client(http)

    client.send_card({}, receive_id="ou_x")

    _, kwargs = http.request.call_args
    assert kwargs["headers"]["Authorization"] == "Bearer t_test"


# ----------------------------------------------------------------------
# send_text / send_image
# ----------------------------------------------------------------------


def test_send_text_wraps_text_in_json_content() -> None:
    http = MagicMock()
    http.request.return_value = _ok_send_resp("om_text")
    client = _build_client(http)

    msg_id = client.send_text("hello", receive_id="ou_a")

    assert msg_id == "om_text"
    body = http.request.call_args.kwargs["json"]
    assert body["msg_type"] == "text"
    assert json.loads(body["content"]) == {"text": "hello"}


def test_send_image_uploads_then_sends(tmp_path: Path) -> None:
    img = tmp_path / "chart.png"
    img.write_bytes(b"\x89PNG\r\nfake")

    http = MagicMock()
    http.request.side_effect = [
        _resp(200, {"code": 0, "msg": "ok", "data": {"image_key": "img_key_xyz"}}),
        _ok_send_resp("om_img"),
    ]
    client = _build_client(http)

    msg_id = client.send_image(img, receive_id="ou_a")

    assert msg_id == "om_img"
    upload_call = http.request.call_args_list[0]
    assert upload_call.args[1].endswith("/open-apis/im/v1/images")
    assert upload_call.kwargs["data"]["image_type"] == "message"
    assert "image" in upload_call.kwargs["files"]

    send_call = http.request.call_args_list[1]
    body = send_call.kwargs["json"]
    assert body["msg_type"] == "image"
    assert json.loads(body["content"]) == {"image_key": "img_key_xyz"}


def test_send_image_missing_file_raises() -> None:
    client = _build_client(MagicMock())
    with pytest.raises(LarkAPIError, match="file not found"):
        client.send_image(Path("/no/such/file.png"), receive_id="ou_a")


# ----------------------------------------------------------------------
# upload_image
# ----------------------------------------------------------------------


def test_upload_image_returns_image_key(tmp_path: Path) -> None:
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNGfake")

    http = MagicMock()
    http.request.return_value = _resp(
        200, {"code": 0, "msg": "ok", "data": {"image_key": "img_abc"}}
    )
    client = _build_client(http)

    assert client.upload_image(img) == "img_abc"


def test_upload_image_missing_image_key_raises(tmp_path: Path) -> None:
    img = tmp_path / "y.png"
    img.write_bytes(b"x")

    http = MagicMock()
    http.request.return_value = _resp(200, {"code": 0, "msg": "ok", "data": {}})
    client = _build_client(http)

    with pytest.raises(LarkAPIError, match="missing image_key"):
        client.upload_image(img)


# ----------------------------------------------------------------------
# list_chat_messages
# ----------------------------------------------------------------------


def test_list_chat_messages_passes_through_query_args() -> None:
    http = MagicMock()
    http.request.return_value = _resp(
        200, {"code": 0, "msg": "ok", "data": {"items": [{"message_id": "om_1"}]}}
    )
    client = _build_client(http)

    out = client.list_chat_messages(
        chat_id="oc_xyz",
        start_time=1717000000,
        end_time=1717003600,
        page_size=50,
        sort_type="ByCreateTimeAsc",
        page_token="tok",
    )
    assert out == {"items": [{"message_id": "om_1"}]}

    _, kwargs = http.request.call_args
    assert kwargs["params"] == {
        "container_id_type": "chat",
        "container_id": "oc_xyz",
        "page_size": 50,
        "sort_type": "ByCreateTimeAsc",
        "start_time": "1717000000",
        "end_time": "1717003600",
        "page_token": "tok",
    }


def test_list_chat_messages_defaults_when_no_filters() -> None:
    http = MagicMock()
    http.request.return_value = _resp(200, {"code": 0, "data": {}})
    client = _build_client(http)
    client.list_chat_messages(chat_id="oc_x")

    params = http.request.call_args.kwargs["params"]
    assert params["container_id"] == "oc_x"
    assert "start_time" not in params
    assert "end_time" not in params
    assert "page_token" not in params


# ----------------------------------------------------------------------
# resolve_p2p_chat_id
# ----------------------------------------------------------------------


def test_resolve_p2p_chat_id_returns_chat_id_from_send() -> None:
    http = MagicMock()
    http.request.return_value = _ok_send_resp("om_x", chat_id="oc_p2p_x")
    client = _build_client(http)

    chat_id = client.resolve_p2p_chat_id("ou_user")

    assert chat_id == "oc_p2p_x"
    body = http.request.call_args.kwargs["json"]
    assert body["receive_id"] == "ou_user"
    assert body["msg_type"] == "text"


def test_resolve_p2p_chat_id_missing_chat_raises() -> None:
    http = MagicMock()
    http.request.return_value = _resp(
        200, {"code": 0, "msg": "ok", "data": {"message_id": "om_x"}}  # no chat_id
    )
    client = _build_client(http)
    with pytest.raises(LarkAPIError, match="missing chat_id"):
        client.resolve_p2p_chat_id("ou_user")


# ----------------------------------------------------------------------
# error paths
# ----------------------------------------------------------------------


def test_api_code_nonzero_raises_with_msg() -> None:
    http = MagicMock()
    http.request.return_value = _resp(
        200, {"code": 230020, "msg": "no permission to send to this user"}
    )
    client = _build_client(http)
    with pytest.raises(LarkAPIError) as ei:
        client.send_card({}, receive_id="ou_x")
    assert ei.value.code == 230020
    assert "no permission" in ei.value.msg


def test_http_500_raises() -> None:
    http = MagicMock()
    http.request.return_value = _resp(500, raw_text="ise")
    client = _build_client(http)
    with pytest.raises(LarkAPIError) as ei:
        client.send_card({}, receive_id="ou_x")
    assert ei.value.code == 500


def test_transport_exception_raises_with_code_minus_one() -> None:
    http = MagicMock()
    http.request.side_effect = RuntimeError("conn refused")
    client = _build_client(http)
    with pytest.raises(LarkAPIError) as ei:
        client.send_card({}, receive_id="ou_x")
    assert ei.value.code == -1
    assert "conn refused" in ei.value.msg


def test_401_invalidates_token_and_retries_once() -> None:
    http = MagicMock()
    http.request.side_effect = [
        _resp(401, raw_text="invalid token"),
        _ok_send_resp("om_after_refresh"),
    ]
    tm = _FakeTM()
    client = LarkHTTPClient(tm, http_client=http)

    msg_id = client.send_card({}, receive_id="ou_x")

    assert msg_id == "om_after_refresh"
    assert tm.invalidate_count == 1
    assert http.request.call_count == 2


def test_persistent_401_raises_after_one_retry() -> None:
    http = MagicMock()
    http.request.side_effect = [
        _resp(401, raw_text="bad token"),
        _resp(401, raw_text="still bad"),
    ]
    tm = _FakeTM()
    client = LarkHTTPClient(tm, http_client=http)
    with pytest.raises(LarkAPIError) as ei:
        client.send_card({}, receive_id="ou_x")
    assert ei.value.code == 401
    assert tm.invalidate_count == 1
    assert http.request.call_count == 2


# ----------------------------------------------------------------------
# context manager / close
# ----------------------------------------------------------------------


def test_context_manager_closes_owned_client() -> None:
    """LarkHTTPClient created without http_client owns one and closes it on exit."""
    tm = _FakeTM()
    client = LarkHTTPClient(tm)
    fake = MagicMock()
    client._http = fake  # noqa: SLF001 — testing close lifecycle
    with client:
        pass
    fake.close.assert_called_once()
