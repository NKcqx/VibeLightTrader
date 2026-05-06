"""Build a concrete `LLMClient` from `StrategyLLMConfig`.

Single dispatch point. Adding a provider:
  1. Add a module under `llm/`
  2. Add a branch here
  3. Update `StrategyLLMConfig.provider` Literal in `config.py`

We do NOT register clients by string in a global dict — there are only
ever a handful of providers and grouping the dispatch in one function
keeps the import graph obvious (audit-friendly).
"""

from __future__ import annotations

from vibe_trader.llm.anthropic_client import AnthropicClient
from vibe_trader.llm.client import LLMClient
from vibe_trader.llm.cursor_agent import CursorAgentClient
from vibe_trader.llm.openai_compat import OpenAICompatClient


def build_llm_client(
    *,
    provider: str,
    model: str,
    api_key_env: str,
    base_url: str | None = None,
    extra_headers: dict[str, str] | None = None,
    workspace: str | None = None,
    cursor_agent_binary: str = "cursor-agent",
    cursor_agent_extra_flags: tuple[str, ...] = (),
) -> LLMClient:
    """Resolve `(provider, model, ...)` into an LLMClient.

    Raises ValueError on unknown provider; raises ValueError when
    `provider == "openai_compat"` is missing `base_url`.
    """
    if provider == "anthropic":
        return AnthropicClient(
            model=model,
            api_key_env=api_key_env or "ANTHROPIC_API_KEY",
            base_url=base_url,
        )

    if provider == "openai_compat":
        if not base_url:
            raise ValueError(
                "provider=openai_compat requires base_url "
                "(e.g. https://api.deepseek.com, "
                "https://ark.cn-beijing.volces.com/api/v3, "
                "https://openrouter.ai/api/v1, http://localhost:11434/v1)"
            )
        return OpenAICompatClient(
            model=model,
            base_url=base_url,
            api_key_env=api_key_env,
            extra_headers=extra_headers,
        )

    if provider == "cursor-agent":
        # No api_key — cursor-agent uses the IDE-logged-in session.
        # `model` may be empty: when so, the agent uses the user's
        # account default (set in cursor.com dashboard).
        return CursorAgentClient(
            model=model or "",
            workspace=workspace,
            binary=cursor_agent_binary,
            extra_flags=tuple(cursor_agent_extra_flags),
        )

    raise ValueError(
        f"unknown llm provider {provider!r}; "
        "expected one of: anthropic, openai_compat, cursor-agent"
    )
