"""Unit tests for OpenAICompatClient + AnthropicClient.

Real network calls are impossible in CI; tests use httpx's mock transport
to assert request shape and validate happy/error parsing without ever
hitting the real provider.
"""

from __future__ import annotations

import httpx
import pytest

from equity_monitor.llm.anthropic_client import AnthropicClient
from equity_monitor.llm.client import (
    LLMAuthError,
    LLMHTTPError,
    LLMRateLimitError,
    LLMTimeoutError,
)
from equity_monitor.llm.openai_compat import OpenAICompatClient


# ---------------------------------------------------------------------------
# OpenAICompatClient
# ---------------------------------------------------------------------------


def _patch_httpx(monkeypatch, response: httpx.Response | Exception) -> list[dict]:
    """Replace httpx.post with a recorder; return list captured calls.

    `response` is what we pretend the server returned (or raised).
    """
    captured: list[dict] = []

    def fake_post(url, json=None, headers=None, timeout=None, **_kw):
        captured.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(httpx, "post", fake_post)
    return captured


def _resp(status: int, body: dict | str) -> httpx.Response:
    if isinstance(body, dict):
        return httpx.Response(status, json=body)
    return httpx.Response(status, text=body)


def test_openai_compat_happy_path(monkeypatch) -> None:
    body = {
        "choices": [
            {"message": {"content": "  hi there  "}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 12, "completion_tokens": 7},
    }
    captured = _patch_httpx(monkeypatch, _resp(200, body))
    monkeypatch.setenv("FAKE_KEY", "sk-fake")

    client = OpenAICompatClient(
        model="m-1", base_url="https://api.deepseek.com", api_key_env="FAKE_KEY"
    )
    out = client.chat(
        [{"role": "user", "content": "hi"}],
        max_tokens=64, temperature=0.0, timeout_s=10.0,
    )
    assert out.text == "hi there"
    assert out.prompt_tokens == 12
    assert out.completion_tokens == 7
    assert out.finish_reason == "stop"
    assert captured[0]["url"] == "https://api.deepseek.com/chat/completions"
    assert captured[0]["json"]["model"] == "m-1"
    assert captured[0]["headers"]["Authorization"] == "Bearer sk-fake"


def test_openai_compat_omits_auth_header_when_no_key(monkeypatch) -> None:
    captured = _patch_httpx(
        monkeypatch,
        _resp(200, {"choices": [{"message": {"content": "ok"}}]}),
    )
    client = OpenAICompatClient(
        model="qwen2.5", base_url="http://localhost:11434/v1", api_key_env=""
    )
    client.chat([{"role": "user", "content": "x"}], max_tokens=8, temperature=0, timeout_s=5)
    assert "Authorization" not in captured[0]["headers"]


def test_openai_compat_401_raises_auth(monkeypatch) -> None:
    _patch_httpx(monkeypatch, _resp(401, {"error": {"message": "bad key"}}))
    client = OpenAICompatClient(model="m", base_url="https://x", api_key_env="FAKE_KEY")
    with pytest.raises(LLMAuthError):
        client.chat([{"role": "user", "content": "x"}], max_tokens=8, temperature=0, timeout_s=5)


def test_openai_compat_429_raises_ratelimit(monkeypatch) -> None:
    _patch_httpx(monkeypatch, _resp(429, "Too Many"))
    client = OpenAICompatClient(model="m", base_url="https://x", api_key_env="FAKE_KEY")
    with pytest.raises(LLMRateLimitError):
        client.chat([{"role": "user", "content": "x"}], max_tokens=8, temperature=0, timeout_s=5)


def test_openai_compat_500_raises_http(monkeypatch) -> None:
    _patch_httpx(monkeypatch, _resp(500, "boom"))
    client = OpenAICompatClient(model="m", base_url="https://x", api_key_env="FAKE_KEY")
    with pytest.raises(LLMHTTPError) as ei:
        client.chat([{"role": "user", "content": "x"}], max_tokens=8, temperature=0, timeout_s=5)
    assert ei.value.status_code == 500


def test_openai_compat_timeout(monkeypatch) -> None:
    _patch_httpx(monkeypatch, httpx.TimeoutException("slow"))
    client = OpenAICompatClient(model="m", base_url="https://x", api_key_env="FAKE_KEY")
    with pytest.raises(LLMTimeoutError):
        client.chat([{"role": "user", "content": "x"}], max_tokens=8, temperature=0, timeout_s=5)


# ---------------------------------------------------------------------------
# AnthropicClient
# ---------------------------------------------------------------------------


def test_anthropic_happy_path_extracts_text_and_usage(monkeypatch) -> None:
    body = {
        "content": [{"type": "text", "text": "decision time"}],
        "usage": {"input_tokens": 100, "output_tokens": 12},
        "stop_reason": "end_turn",
    }
    captured = _patch_httpx(monkeypatch, _resp(200, body))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    client = AnthropicClient(model="claude-3-5-haiku-20241022")
    out = client.chat(
        [
            {"role": "system", "content": "you are X"},
            {"role": "user", "content": "hi"},
        ],
        max_tokens=128, temperature=0.0, timeout_s=10.0,
    )
    assert out.text == "decision time"
    assert out.prompt_tokens == 100
    assert out.completion_tokens == 12
    assert out.finish_reason == "end_turn"

    # Verify Anthropic-specific request shape:
    sent = captured[0]
    assert sent["headers"]["x-api-key"] == "sk-ant-test"
    assert sent["headers"]["anthropic-version"]
    assert sent["json"]["system"] == "you are X"  # split out of messages
    assert sent["json"]["messages"] == [{"role": "user", "content": "hi"}]


def test_anthropic_missing_api_key_raises_auth_without_http_call(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client = AnthropicClient(model="claude-3-5-haiku-20241022")
    captured = _patch_httpx(monkeypatch, _resp(200, {}))  # would crash if called
    with pytest.raises(LLMAuthError):
        client.chat(
            [{"role": "user", "content": "hi"}],
            max_tokens=8, temperature=0.0, timeout_s=5.0,
        )
    assert captured == [], "must short-circuit before issuing HTTP request"


def test_anthropic_concatenates_multiple_text_blocks(monkeypatch) -> None:
    body = {
        "content": [
            {"type": "text", "text": "part1 "},
            {"type": "tool_use", "id": "tu_1"},  # ignored
            {"type": "text", "text": "part2"},
        ],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    _patch_httpx(monkeypatch, _resp(200, body))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    client = AnthropicClient(model="m")
    out = client.chat(
        [{"role": "user", "content": "x"}],
        max_tokens=8, temperature=0.0, timeout_s=5.0,
    )
    assert out.text == "part1 part2"


def test_anthropic_429_raises_ratelimit(monkeypatch) -> None:
    _patch_httpx(monkeypatch, _resp(429, "slow down"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    client = AnthropicClient(model="m")
    with pytest.raises(LLMRateLimitError):
        client.chat(
            [{"role": "user", "content": "x"}],
            max_tokens=8, temperature=0.0, timeout_s=5.0,
        )
