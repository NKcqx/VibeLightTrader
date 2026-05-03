from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from tenacity import wait_none

from equity_monitor.reports.lark_image import LarkImageError, send_image


@pytest.fixture(autouse=True)
def _no_retry_wait(monkeypatch):
    """Make tenacity not sleep between retries — matches test_reports_lark.py."""
    monkeypatch.setattr(send_image.retry, "wait", wait_none())
    yield


def test_send_image_invokes_lark_cli_and_returns_msg_id(tmp_path, monkeypatch) -> None:
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    captured: dict = {}

    class FakeRes:
        returncode = 0
        stdout = json.dumps({"ok": True, "data": {"message_id": "om_xxx"}})
        stderr = ""

    def fake_run(args, **kw):
        captured["args"] = args
        return FakeRes()

    monkeypatch.setattr(subprocess, "run", fake_run)
    msg_id = send_image(img, open_id="ou_abc", receiver_type="user")
    assert msg_id == "om_xxx"
    # Argv must include the lark-cli command, the user-id flag, and --image with the absolute path
    args = captured["args"]
    assert args[0:3] == ["lark-cli", "im", "+messages-send"]
    assert "--user-id" in args
    assert args[args.index("--user-id") + 1] == "ou_abc"
    assert "--image" in args
    assert args[args.index("--image") + 1] == str(img.absolute())
    assert "--as" in args
    assert args[args.index("--as") + 1] == "bot"


def test_send_image_chat_receiver_uses_chat_id_flag(tmp_path, monkeypatch) -> None:
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG")
    captured: dict = {}

    class FakeRes:
        returncode = 0
        stdout = json.dumps({"ok": True, "data": {"message_id": "om_yyy"}})
        stderr = ""

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda args, **kw: (captured.update(args=args), FakeRes())[1],
    )
    msg_id = send_image(img, open_id="oc_xyz", receiver_type="chat")
    assert msg_id == "om_yyy"
    args = captured["args"]
    assert "--chat-id" in args
    assert args[args.index("--chat-id") + 1] == "oc_xyz"
    assert "--user-id" not in args


def test_send_image_raises_on_nonzero_rc(tmp_path, monkeypatch) -> None:
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG")

    class BadRes:
        returncode = 7
        stdout = ""
        stderr = "boom\n"

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: BadRes())
    with pytest.raises(LarkImageError, match="boom"):
        send_image(img, open_id="ou_abc", receiver_type="user")


def test_send_image_raises_on_ok_false_response(tmp_path, monkeypatch) -> None:
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG")

    class FakeRes:
        returncode = 0
        stdout = json.dumps(
            {
                "ok": False,
                "error": {"type": "permission_denied", "message": "missing scope"},
            }
        )
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: FakeRes())
    with pytest.raises(LarkImageError, match="permission_denied"):
        send_image(img, open_id="ou_abc", receiver_type="user")


def test_send_image_raises_on_unparseable_response(tmp_path, monkeypatch) -> None:
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG")

    class FakeRes:
        returncode = 0
        stdout = "not-json"
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: FakeRes())
    with pytest.raises(LarkImageError, match="non-JSON"):
        send_image(img, open_id="ou_abc", receiver_type="user")


def test_send_image_raises_on_missing_file() -> None:
    with pytest.raises(LarkImageError, match="file not found"):
        send_image(Path("/tmp/nonexistent_xyz_p3.png"), open_id="ou", receiver_type="user")


def test_send_image_raises_on_unknown_receiver_type(tmp_path) -> None:
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG")
    with pytest.raises(LarkImageError, match="unknown receiver_type"):
        send_image(img, open_id="x", receiver_type="bogus")  # type: ignore[arg-type]
