"""Anti-regression: stream_lark_events_ws must spawn `event consume`,
not the deprecated `+subscribe` (rejected by lark-cli ≥1.0.23 server).
"""

from __future__ import annotations

import io
import subprocess

import pytest

from equity_monitor.events.listener import stream_lark_events_ws


class _FakePopen:
    """Minimal Popen stub: records argv, yields one synthetic event so the
    generator can advance past the first iteration of its infinite loop,
    then EOFs cleanly.
    """

    captured_cmds: list[list[str]] = []

    def __init__(self, cmd, **kw):
        type(self).captured_cmds.append(cmd)
        self.stdout = io.StringIO('{"event_type":"_probe"}\n')
        self.returncode = 0

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


@pytest.fixture(autouse=True)
def _reset_captured():
    _FakePopen.captured_cmds = []
    yield


def test_spawn_uses_consume_subcommand_not_subscribe(monkeypatch) -> None:
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)

    gen = stream_lark_events_ws()
    next(gen, None)
    gen.close()

    assert _FakePopen.captured_cmds, "Popen must have been called at least once"
    cmd = _FakePopen.captured_cmds[0]

    assert cmd[0] == "lark-cli"
    assert cmd[1] == "event"
    assert cmd[2] == "consume", f"must use 'consume', got: {cmd!r}"

    assert "+subscribe" not in cmd, (
        f"+subscribe is deprecated; server rejects it with 'another instance is "
        f"already running'. Got: {cmd!r}"
    )
    assert "--event-types" not in cmd, (
        f"--event-types belongs to the old +subscribe API. The new consume "
        f"API takes EventKey as a positional. Got: {cmd!r}"
    )


def test_spawn_passes_event_key_as_positional(monkeypatch) -> None:
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)

    gen = stream_lark_events_ws(event_key="im.message.receive_v1")
    next(gen, None)
    gen.close()

    cmd = _FakePopen.captured_cmds[0]
    consume_idx = cmd.index("consume")
    assert cmd[consume_idx + 1] == "im.message.receive_v1", (
        f"EventKey must immediately follow 'consume'; got: {cmd!r}"
    )


def test_spawn_includes_as_identity_flag(monkeypatch) -> None:
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)

    gen = stream_lark_events_ws(identity="bot")
    next(gen, None)
    gen.close()

    cmd = _FakePopen.captured_cmds[0]
    assert "--as" in cmd
    assert cmd[cmd.index("--as") + 1] == "bot"


def test_spawn_honors_custom_cli_path(monkeypatch) -> None:
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)

    gen = stream_lark_events_ws(cli_path="/usr/local/bin/lark-cli")
    next(gen, None)
    gen.close()

    cmd = _FakePopen.captured_cmds[0]
    assert cmd[0] == "/usr/local/bin/lark-cli"
