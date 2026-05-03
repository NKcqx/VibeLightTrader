"""LLM client contract — Protocol, response shape, error hierarchy.

Concrete clients live in `openai_compat.py` / `anthropic_client.py`. The
strategy layer (`signals/strategy_llm.py`) only depends on this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TypedDict, runtime_checkable


class Message(TypedDict):
    """One chat message in OpenAI-style format.

    Anthropic clients translate this internally (system → top-level
    `system` parameter; user/assistant → `messages` array).
    """

    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True)
class LLMResponse:
    """Provider-agnostic completion result.

    `raw` is the full provider payload preserved verbatim — useful for
    audit logs and post-hoc debugging. Token counts are best-effort
    (some providers omit them; we record None instead of fabricating).
    """

    text: str
    """The assistant's textual reply, stripped of leading/trailing
    whitespace. JSON parsing happens upstream in prompt.py."""

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    finish_reason: str | None = None
    """Provider-reported reason: stop / length / tool_use / etc."""

    raw: dict[str, Any] = field(default_factory=dict)
    """Raw provider response — keep for audit, don't render in cards."""


# ---------------------------------------------------------------------------
# Errors. Strategy layer maps these to its `fallback_on_error` policy
# (rule|hold). Order matters: more-specific subclasses come first so
# strategy code can `except LLMTimeoutError` without also catching
# parse failures.
# ---------------------------------------------------------------------------


class LLMError(Exception):
    """Base for all LLM client / parsing failures.

    Carries an optional `provider` tag for audit logs (so a stack trace
    on Anthropic is distinguishable from one on OpenRouter).
    """

    def __init__(self, message: str, *, provider: str | None = None) -> None:
        super().__init__(message)
        self.provider = provider


class LLMTimeoutError(LLMError):
    """The HTTP call exceeded the configured timeout."""


class LLMHTTPError(LLMError):
    """Non-2xx HTTP status from the provider; carries status + body for log."""

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        status_code: int | None = None,
        body: str | None = None,
    ) -> None:
        super().__init__(message, provider=provider)
        self.status_code = status_code
        self.body = body


class LLMAuthError(LLMHTTPError):
    """401/403 — bad / missing API key. Don't retry."""


class LLMRateLimitError(LLMHTTPError):
    """429 — rate-limited. Caller may choose to retry with backoff."""


class LLMParseError(LLMError):
    """LLM returned text that couldn't be parsed into a Decision JSON."""


@runtime_checkable
class LLMClient(Protocol):
    """Provider-agnostic chat completion client.

    Implementations MUST be stateless and thread-safe (we may run
    multiple symbols in parallel). They should NOT cache responses —
    caching is the strategy layer's job (it knows the right cache key).
    """

    name: str
    """Stable identifier for audit logs, e.g. "anthropic:claude-3-5-sonnet"
    or "openai_compat:https://api.deepseek.com/deepseek-chat"."""

    model: str

    def chat(
        self,
        messages: list[Message],
        *,
        max_tokens: int,
        temperature: float,
        timeout_s: float,
    ) -> LLMResponse:
        """Run one chat completion.

        Implementations MUST raise:
          - LLMTimeoutError on httpx.TimeoutException / asyncio.TimeoutError
          - LLMAuthError on HTTP 401/403
          - LLMRateLimitError on HTTP 429
          - LLMHTTPError on any other non-2xx
          - LLMError on transport failures, malformed payload, etc.
        """
        ...
