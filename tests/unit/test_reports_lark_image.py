"""Unit tests for the HTTP-backed reports.lark_image facade."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from tenacity import wait_none

from vibe_trader.lark.errors import LarkAPIError
from vibe_trader.reports.lark_image import LarkImageError, send_image


@pytest.fixture(autouse=True)
def _no_retry_wait(monkeypatch):
    """Make tenacity not sleep between retries."""
    monkeypatch.setattr(send_image.retry, "wait", wait_none())
    yield


def _client_returning(msg_id: str = "om_img") -> MagicMock:
    c = MagicMock()
    c.send_image.return_value = msg_id
    return c


def test_send_image_returns_message_id(tmp_path: Path) -> None:
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG")
    client = _client_returning("om_xxx")
    assert send_image(img, open_id="ou_a", client=client) == "om_xxx"


def test_send_image_user_maps_to_open_id_receive_type(tmp_path: Path) -> None:
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG")
    client = _client_returning()
    send_image(img, open_id="ou_abc", receiver_type="user", client=client)
    call = client.send_image.call_args
    assert call.args[0] == img
    assert call.kwargs == {"receive_id": "ou_abc", "receive_id_type": "open_id"}


def test_send_image_chat_maps_to_chat_id_receive_type(tmp_path: Path) -> None:
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG")
    client = _client_returning()
    send_image(img, open_id="oc_xyz", receiver_type="chat", client=client)
    call = client.send_image.call_args
    assert call.kwargs == {"receive_id": "oc_xyz", "receive_id_type": "chat_id"}


def test_send_image_raises_on_missing_file() -> None:
    client = MagicMock()
    with pytest.raises(LarkImageError, match="file not found"):
        send_image(
            Path("/tmp/nonexistent_xyz.png"), open_id="ou", client=client
        )


def test_send_image_wraps_api_error(tmp_path: Path) -> None:
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG")
    client = MagicMock()
    client.send_image.side_effect = LarkAPIError(230020, "permission denied")
    with pytest.raises(LarkImageError, match="permission denied"):
        send_image(img, open_id="ou", client=client)


def test_send_image_retries_three_times_on_transient_error(tmp_path: Path) -> None:
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG")
    client = MagicMock()
    client.send_image.side_effect = LarkAPIError(-1, "boom")
    with pytest.raises(LarkImageError, match="boom"):
        send_image(img, open_id="ou", client=client)
    assert client.send_image.call_count == 3


def test_send_image_unknown_receiver_type_rejected(tmp_path: Path) -> None:
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG")
    client = MagicMock()
    with pytest.raises(LarkImageError, match="unknown receiver_type"):
        send_image(img, open_id="x", receiver_type="bogus", client=client)  # type: ignore[arg-type]
