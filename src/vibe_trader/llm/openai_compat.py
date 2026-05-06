"""OpenAI-compatible chat completion client.

A single REST-shape covers OpenAI, DeepSeek, 字节豆包 (volcengine ARK),
OpenRouter, Ollama, and vLLM-served models. Differences are entirely in
`base_url` and `model` strings supplied by config.

Synchronous (httpx.post) by design — strategy layer parallelises across
symbols via thread pool, not asyncio.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from vibe_trader.llm.client import (
    LLMAuthError,
    LLMError,
    LLMHTTPError,
    LLMRateLimitError,
    LLMResponse,
    LLMTimeoutError,
    Message,
)


class OpenAICompatClient:
    """Chat client targeting any /v1/chat/completions endpoint.

    Args:
        model: model id ("gpt-4o", "deepseek-chat", "qwen2.5:32b", etc.)
        base_url: scheme+host+path-prefix WITHOUT trailing slash, e.g.
                  "https://api.deepseek.com" or "http://localhost:11434/v1".
                  We append "/chat/completions" — providers vary on
                  whether the prefix already includes "/v1", so we do
                  NOT auto-insert it; just paste the URL their docs gave
                  you.
        api_key_env: env var name to read the API key from. Empty / unset
                  is allowed (Ollama doesn't need one); we send the
                  Authorization header only when a key is present.
        extra_headers: optional dict for provider quirks (OpenRouter
                  recommends HTTP-Referer / X-Title).
    """

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key_env: str = "",
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("OpenAICompatClient: base_url is required")
        self.model = model
        self._base_url = base_url.rstrip("/")
        self._api_key = os.environ.get(api_key_env, "") if api_key_env else ""
        self._extra_headers = dict(extra_headers or {})
        # Stable id for audit log, e.g. "openai_compat:api.deepseek.com/deepseek-chat"
        host = self._base_url.split("//", 1)[-1].split("/", 1)[0]
        self.name = f"openai_compat:{host}/{model}"

    def chat(
        self,
        messages: list[Message],
        *,
        max_tokens: int,
        temperature: float,
        timeout_s: float,
    ) -> LLMResponse:
        url = f"{self._base_url}/chat/completions"
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        headers = {"Content-Type": "application/json", **self._extra_headers}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        try:
            resp = httpx.post(url, json=payload, headers=headers, timeout=timeout_s)
        except httpx.TimeoutException as e:
            raise LLMTimeoutError(
                f"timeout after {timeout_s}s", provider=self.name
            ) from e
        except httpx.HTTPError as e:
            raise LLMError(f"transport failure: {e}", provider=self.name) from e

        if resp.status_code in (401, 403):
            raise LLMAuthError(
                f"auth rejected by {self.name}",
                provider=self.name,
                status_code=resp.status_code,
                body=resp.text[:500],
            )
        if resp.status_code == 429:
            raise LLMRateLimitError(
                f"rate-limited by {self.name}",
                provider=self.name,
                status_code=resp.status_code,
                body=resp.text[:500],
            )
        if resp.status_code >= 400:
            raise LLMHTTPError(
                f"http {resp.status_code} from {self.name}",
                provider=self.name,
                status_code=resp.status_code,
                body=resp.text[:500],
            )

        try:
            data = resp.json()
        except ValueError as e:
            raise LLMError(
                f"non-JSON response from {self.name}", provider=self.name
            ) from e

        try:
            choice = data["choices"][0]
            text = (choice["message"]["content"] or "").strip()
            finish = choice.get("finish_reason")
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(
                f"malformed response from {self.name}: {data!r}",
                provider=self.name,
            ) from e

        usage = data.get("usage") or {}
        return LLMResponse(
            text=text,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            finish_reason=finish,
            raw=data,
        )


# OpenAICompatClient satisfies LLMClient via duck typing; no runtime
# isinstance check needed because Protocol is runtime_checkable elsewhere.
