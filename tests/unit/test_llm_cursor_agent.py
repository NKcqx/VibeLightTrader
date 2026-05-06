"""Unit tests for the cursor-agent LLMClient adapter.

We never spawn the real binary in CI. `subprocess.run` is monkey-patched
to a fake that replays canned stdout/returncode/exception, so all we
verify here is the args we hand the CLI, and that we map the JSON
envelope back to LLMResponse / LLMError correctly.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from vibe_trader.llm.client import (
    LLMError,
    LLMHTTPError,
    LLMParseError,
    LLMTimeoutError,
)
from vibe_trader.llm.cursor_agent import CursorAgentClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeCompletedProcess:
    def __init__(self, *, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _patch_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    *,
    result: _FakeCompletedProcess | Exception,
) -> list[list[str]]:
    """Replace subprocess.run with a recorder. Returns the list it appends to.

    `result`:
      - _FakeCompletedProcess → returned verbatim
      - Exception             → raised
    """
    captured: list[list[str]] = []

    def fake_run(args, **_kwargs):
        captured.append(list(args))
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        "vibe_trader.llm.cursor_agent.shutil.which",
        lambda _name: "/fake/path/cursor-agent",
    )
    return captured


def _envelope(text: str, **overrides: Any) -> str:
    body: dict[str, Any] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 1234,
        "duration_api_ms": 1234,
        "result": text,
        "session_id": "sess-1",
        "request_id": "req-1",
        "usage": {
            "inputTokens": 100,
            "outputTokens": 50,
            "cacheReadTokens": 0,
            "cacheWriteTokens": 0,
        },
    }
    body.update(overrides)
    return json.dumps(body)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_chat_happy_path_parses_envelope_and_emits_args(monkeypatch):
    out_text = (
        "Decision:\n"
        "```json\n"
        '{"action":"BUY","qty":50,"confidence":0.8,"reason":"oversold"}\n'
        "```"
    )
    captured = _patch_subprocess(
        monkeypatch,
        result=_FakeCompletedProcess(returncode=0, stdout=_envelope(out_text)),
    )
    client = CursorAgentClient(
        model="sonnet-4",
        workspace="/tmp/repo",
        extra_flags=("--mode", "plan"),
    )
    response = client.chat(
        [
            {"role": "system", "content": "you are a trading agent"},
            {"role": "user", "content": "what now?"},
        ],
        max_tokens=512,
        temperature=0.0,
        timeout_s=60.0,
    )

    assert response.text.startswith("Decision:")
    assert response.prompt_tokens == 100
    assert response.completion_tokens == 50
    assert response.finish_reason == "success"
    assert response.raw["session_id"] == "sess-1"

    args = captured[0]
    assert args[0] == "/fake/path/cursor-agent"
    assert "--print" in args
    assert "--output-format" in args and args[args.index("--output-format") + 1] == "json"
    assert "--trust" in args
    assert "--model" in args and args[args.index("--model") + 1] == "sonnet-4"
    assert "--workspace" in args
    assert args[args.index("--workspace") + 1] == "/tmp/repo"
    assert "--mode" in args and "plan" in args
    assert "-p" in args
    prompt = args[args.index("-p") + 1]
    assert "[System]" in prompt
    assert "you are a trading agent" in prompt
    assert "what now?" in prompt


def test_chat_omits_model_flag_when_empty(monkeypatch):
    captured = _patch_subprocess(
        monkeypatch,
        result=_FakeCompletedProcess(returncode=0, stdout=_envelope("ok")),
    )
    client = CursorAgentClient(model="", workspace=None)
    client.chat(
        [{"role": "user", "content": "hi"}],
        max_tokens=10,
        temperature=0.0,
        timeout_s=10.0,
    )
    args = captured[0]
    assert "--model" not in args
    assert "--workspace" not in args


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


def test_chat_timeout_raises_llm_timeout_error(monkeypatch):
    _patch_subprocess(
        monkeypatch,
        result=subprocess.TimeoutExpired(cmd=["cursor-agent"], timeout=5),
    )
    client = CursorAgentClient()
    with pytest.raises(LLMTimeoutError) as excinfo:
        client.chat(
            [{"role": "user", "content": "hi"}],
            max_tokens=10,
            temperature=0.0,
            timeout_s=5.0,
        )
    assert "5.0" in str(excinfo.value) or "5s" in str(excinfo.value)


def test_chat_nonzero_exit_raises_http_error(monkeypatch):
    _patch_subprocess(
        monkeypatch,
        result=_FakeCompletedProcess(
            returncode=1,
            stdout="",
            stderr="not logged in. run `cursor-agent login`",
        ),
    )
    client = CursorAgentClient()
    with pytest.raises(LLMHTTPError) as excinfo:
        client.chat(
            [{"role": "user", "content": "hi"}],
            max_tokens=10,
            temperature=0.0,
            timeout_s=10.0,
        )
    assert "not logged in" in str(excinfo.value)
    assert excinfo.value.status_code == 1


def test_chat_is_error_envelope_raises_http_error(monkeypatch):
    bad = _envelope(
        "internal model failure",
        is_error=True,
        subtype="error",
    )
    _patch_subprocess(
        monkeypatch,
        result=_FakeCompletedProcess(returncode=0, stdout=bad),
    )
    client = CursorAgentClient()
    with pytest.raises(LLMHTTPError) as excinfo:
        client.chat(
            [{"role": "user", "content": "hi"}],
            max_tokens=10,
            temperature=0.0,
            timeout_s=10.0,
        )
    assert "internal model failure" in str(excinfo.value)


def test_chat_invalid_json_raises_parse_error(monkeypatch):
    _patch_subprocess(
        monkeypatch,
        result=_FakeCompletedProcess(returncode=0, stdout="not even json"),
    )
    client = CursorAgentClient()
    with pytest.raises(LLMParseError):
        client.chat(
            [{"role": "user", "content": "hi"}],
            max_tokens=10,
            temperature=0.0,
            timeout_s=10.0,
        )


def test_chat_envelope_missing_result_raises_parse_error(monkeypatch):
    raw = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "usage": {"inputTokens": 1, "outputTokens": 0},
            # 'result' missing
        }
    )
    _patch_subprocess(
        monkeypatch,
        result=_FakeCompletedProcess(returncode=0, stdout=raw),
    )
    client = CursorAgentClient()
    with pytest.raises(LLMParseError):
        client.chat(
            [{"role": "user", "content": "hi"}],
            max_tokens=10,
            temperature=0.0,
            timeout_s=10.0,
        )


def test_chat_empty_stdout_raises(monkeypatch):
    _patch_subprocess(
        monkeypatch,
        result=_FakeCompletedProcess(returncode=0, stdout="   "),
    )
    client = CursorAgentClient()
    with pytest.raises(LLMError):
        client.chat(
            [{"role": "user", "content": "hi"}],
            max_tokens=10,
            temperature=0.0,
            timeout_s=10.0,
        )


def test_chat_strips_ansi_prefix_before_json(monkeypatch):
    """cursor-agent occasionally emits ANSI control sequences before JSON."""
    out = "\x1b[2K\x1b[1G" + _envelope("ok")
    _patch_subprocess(
        monkeypatch,
        result=_FakeCompletedProcess(returncode=0, stdout=out),
    )
    client = CursorAgentClient()
    response = client.chat(
        [{"role": "user", "content": "hi"}],
        max_tokens=10,
        temperature=0.0,
        timeout_s=10.0,
    )
    assert response.text == "ok"


def test_resolve_binary_missing_raises_llm_error(monkeypatch):
    monkeypatch.setattr(
        "vibe_trader.llm.cursor_agent.shutil.which",
        lambda _name: None,
    )
    client = CursorAgentClient()
    with pytest.raises(LLMError) as excinfo:
        client.chat(
            [{"role": "user", "content": "hi"}],
            max_tokens=10,
            temperature=0.0,
            timeout_s=10.0,
        )
    assert "not on PATH" in str(excinfo.value)


def test_name_property_includes_model_or_default():
    assert CursorAgentClient(model="sonnet-4").name == "cursor-agent:sonnet-4"
    assert CursorAgentClient(model="").name == "cursor-agent:default"
