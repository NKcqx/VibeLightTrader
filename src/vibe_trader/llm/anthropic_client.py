"""Anthropic Claude REST client (no SDK).

Uses the Messages API (https://api.anthropic.com/v1/messages). The
public surface mirrors `OpenAICompatClient` so the strategy layer is
provider-agnostic; differences vs OpenAI:

  - Anthropic puts the system prompt at the top level (`system: "..."`),
    NOT inside the `messages` array. We split it on the way in.
  - The response shape is `content: [{type: "text", text: "..."}]`
    instead of OpenAI's `choices[0].message.content`.
  - Auth uses `x-api-key` header + `anthropic-version` (date).
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

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_VERSION = "2023-06-01"
"""Pinned API version. Bump only after manually validating against the
Anthropic changelog — newer versions have made breaking shape changes
in the past."""


class AnthropicClient:
    """Claude chat client (Sonnet/Opus/Haiku).

    Args:
        model: model id, e.g. "claude-3-5-sonnet-20241022",
               "claude-3-5-haiku-20241022", "claude-3-opus-20240229"
               (substitute the latest IDs from console.anthropic.com).
        api_key_env: env var holding the API key (default
                     "ANTHROPIC_API_KEY"). Empty / unset is allowed at
                     construction time so unit tests can stub the
                     transport, but `chat()` will raise `LLMAuthError`
                     when actually called.
        base_url: override for proxies / regional endpoints. Most users
                  leave this as-is (= ANTHROPIC_API_URL).
    """

    def __init__(
        self,
        *,
        model: str,
        api_key_env: str = "ANTHROPIC_API_KEY",
        base_url: str | None = None,
    ) -> None:
        self.model = model
        self._api_key = os.environ.get(api_key_env, "") if api_key_env else ""
        self._base_url = base_url or ANTHROPIC_API_URL
        self.name = f"anthropic:{model}"

    def chat(
        self,
        messages: list[Message],
        *,
        max_tokens: int,
        temperature: float,
        timeout_s: float,
    ) -> LLMResponse:
        if not self._api_key:
            raise LLMAuthError(
                f"missing api_key for {self.name}; set ANTHROPIC_API_KEY",
                provider=self.name,
                status_code=401,
            )

        # Split system prompt out — Anthropic's API requires it at the top level.
        system_parts = [m["content"] for m in messages if m["role"] == "system"]
        chat_msgs = [
            {"role": m["role"], "content": m["content"]}
            for m in messages
            if m["role"] in ("user", "assistant")
        ]
        if not chat_msgs:
            raise LLMError(
                "Anthropic requires at least one user message", provider=self.name
            )

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": chat_msgs,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)

        headers = {
            "Content-Type": "application/json",
            "x-api-key": self._api_key,
            "anthropic-version": ANTHROPIC_API_VERSION,
        }

        try:
            resp = httpx.post(
                self._base_url, json=payload, headers=headers, timeout=timeout_s
            )
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
            # content is a list of blocks; we concat any text blocks.
            blocks = data.get("content") or []
            text_parts = [
                b.get("text", "")
                for b in blocks
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            text = "".join(text_parts).strip()
            if not text:
                raise LLMError(
                    f"empty response text from {self.name}: {data!r}",
                    provider=self.name,
                )
            finish = data.get("stop_reason")
        except (KeyError, TypeError, AttributeError) as e:
            raise LLMError(
                f"malformed response from {self.name}: {data!r}",
                provider=self.name,
            ) from e

        usage = data.get("usage") or {}
        return LLMResponse(
            text=text,
            prompt_tokens=usage.get("input_tokens"),
            completion_tokens=usage.get("output_tokens"),
            finish_reason=finish,
            raw=data,
        )
