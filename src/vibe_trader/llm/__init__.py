"""LLM client abstraction for the strategy layer.

This package is consumed exclusively by `signals/strategy_llm.py`. It is
deliberately split into:

  - `client`         protocol + response + error types
  - `openai_compat`  REST client for any OpenAI-API-compatible endpoint
                     (OpenAI, DeepSeek, Doubao, OpenRouter, Ollama, vLLM, ...)
  - `anthropic_client` REST client for Anthropic Claude
  - `factory`        StrategyLLMConfig → concrete LLMClient
  - `prompt`         jinja2 prompt template + JSON-tolerant parser

Adding a new provider = one new module + one branch in `factory`. The
strategy layer never imports a concrete client.
"""

from vibe_trader.llm.client import (
    LLMClient,
    LLMError,
    LLMParseError,
    LLMResponse,
    LLMTimeoutError,
    Message,
)
from vibe_trader.llm.factory import build_llm_client
from vibe_trader.llm.prompt import (
    DEFAULT_SYSTEM_PROMPT,
    DEFAULT_USER_TEMPLATE,
    ParsedDecision,
    parse_decision,
    render_user_prompt,
)

__all__ = [
    "LLMClient",
    "LLMError",
    "LLMParseError",
    "LLMResponse",
    "LLMTimeoutError",
    "Message",
    "build_llm_client",
    "DEFAULT_SYSTEM_PROMPT",
    "DEFAULT_USER_TEMPLATE",
    "ParsedDecision",
    "parse_decision",
    "render_user_prompt",
]
