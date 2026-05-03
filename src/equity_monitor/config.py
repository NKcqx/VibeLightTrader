from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator


class SymbolConfig(BaseModel):
    code: str = Field(pattern=r"^(US|HK|SH|SZ)\.[A-Z0-9._-]+$")
    name: str
    upper_threshold: float | None = None
    lower_threshold: float | None = None
    notes: str | None = None

    @field_validator("upper_threshold", "lower_threshold")
    @classmethod
    def _positive(cls, v: float | None) -> float | None:
        if v is not None and v <= 0:
            raise ValueError("threshold must be positive")
        return v


class WatchlistConfig(BaseModel):
    symbols: list[SymbolConfig]


class OpenDConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 11111


class DatabaseConfig(BaseModel):
    path: str
    wal_mode: bool = True


class JobCron(BaseModel):
    cron: str


class SchedulerConfig(BaseModel):
    timezone: str
    jobs: dict[str, JobCron]


class LarkReceiver(BaseModel):
    type: Literal["chat", "user"]
    open_id: str


class LarkConfig(BaseModel):
    cli_path: str = "lark-cli"
    receiver: LarkReceiver
    identity: Literal["bot", "user"] = "bot"


class SignalsConfig(BaseModel):
    rsi_overbought: float = 70
    rsi_oversold: float = 30
    bollinger_period: int = 20
    bollinger_std: float = 2
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    dedupe_window_minutes: int = 60
    news_burst_drop: float = 3.0
    news_burst_rise: float = 3.0


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: str | None = None


class StrategyRuleConfig(BaseModel):
    """Knobs for the hard-coded rule strategy (a.k.a. strategy_lite)."""

    max_position_per_symbol: int = 200
    critical_size: int = 100
    warn_size: int = 50
    rsi_extreme: float = 30.0


class StrategyLLMConfig(BaseModel):
    """LLM-driven strategy. Implemented in `signals/strategy_llm.py:LLMStrategy`.

    Switch from rule → llm by setting `trader.strategy.type: llm` AND
    exporting the API key the chosen provider needs:

      - provider=anthropic       → ANTHROPIC_API_KEY
      - provider=openai_compat   → whatever you set `api_key_env` to
                                   (OPENAI_API_KEY, DEEPSEEK_API_KEY,
                                    ARK_API_KEY, OPENROUTER_API_KEY, ...)

    Local Ollama / vLLM servers don't need a key — leave `api_key_env`
    empty (the Authorization header is then omitted).
    """

    provider: Literal["anthropic", "openai_compat"] = "anthropic"
    model: str = "claude-3-5-sonnet-20241022"
    api_key_env: str = "ANTHROPIC_API_KEY"
    base_url: str | None = None
    """Required for `openai_compat`; ignored by `anthropic`."""

    max_tokens: int = 512
    temperature: float = 0.0
    timeout_s: int = 30
    retries: int = 2
    """Reserved — current LLMStrategy does NOT retry. Add when we see
    real rate-limit pressure in production logs."""

    max_position_per_symbol: int = 200
    min_trade_size: int = 10
    min_confidence: float = 0.6
    """Below this confidence, decisions are demoted to HOLD regardless
    of what the LLM said. Sane default: 0.6 (refuse coin-flips)."""

    fallback_on_error: Literal["rule", "hold"] = "rule"
    """When the LLM call/parse/constraint check fails, `rule` falls back
    to RuleStrategy (preserve trading on bad weather). `hold` returns
    HOLD instead — safer if you don't trust the rules either."""

    audit_log_path: str = "data/llm_decisions.jsonl"
    """Append-only NDJSON. One line per decision (LLM-driven OR fallback).
    Inspect with `tail -f data/llm_decisions.jsonl`."""

    kline_window: int = 200
    news_window_minutes: int = 30
    news_top_k: int = 3
    """Reserved for C2b — `_run_strategy_per_code` will fill the
    StrategyContext with this many bars / minutes / news items."""

    cache_seconds: int = 300
    max_concurrent: int = 5
    """`max_concurrent` is reserved (today the strategy runs serially per
    symbol). Will matter once we parallelise LLM calls."""


class StrategyEnsembleConfig(BaseModel):
    """Ensemble strategy. Skeleton only — actual implementation in C3."""

    strategies: list[str] = Field(default_factory=lambda: ["rule"])
    voting: Literal["majority", "weighted", "unanimous"] = "weighted"
    weights: dict[str, float] = Field(default_factory=lambda: {"rule": 1.0})


class StrategyConfig(BaseModel):
    """Active-strategy selector + per-strategy knobs.

    Only the sub-block matching `type` is consumed; the others sit dormant
    and forward-compatible.
    """

    type: Literal["rule", "llm", "ensemble"] = "rule"
    rule: StrategyRuleConfig = Field(default_factory=StrategyRuleConfig)
    llm: StrategyLLMConfig = Field(default_factory=StrategyLLMConfig)
    ensemble: StrategyEnsembleConfig = Field(default_factory=StrategyEnsembleConfig)


class TraderConfig(BaseModel):
    """Paper-trading auto-execution settings."""

    auto_execute: bool = True
    """If True, run_intraday_check executes BUY/SELL suggestions through
    the paper broker and persists Trade/Position rows automatically. If
    False, suggestions are only displayed in the alert card; user runs
    `equity-monitor trade confirm <signal_id>` to execute manually.
    """

    simulate_only: bool = True
    """Hard guard: refuse to operate against any non-SIMULATE account.
    Currently always honored by OpenDSecTrader; flipping False is a
    deliberate, user-acknowledged step toward live trading. Phase 2 keeps
    it pinned True.
    """

    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    """Which strategy decides BUY/SELL/HOLD and with what knobs.

    Default `type=rule` preserves Phase 2 behaviour. Set `type=llm` once
    C2 ships LLMStrategy + you've exported the right API key.
    """


class AppConfig(BaseModel):
    opend: OpenDConfig
    database: DatabaseConfig
    scheduler: SchedulerConfig
    lark: LarkConfig
    signals: SignalsConfig
    logging: LoggingConfig
    trader: TraderConfig = Field(default_factory=TraderConfig)


def load_watchlist(path: str | Path) -> WatchlistConfig:
    data = yaml.safe_load(Path(path).read_text())
    return WatchlistConfig.model_validate(data)


def load_settings(path: str | Path) -> AppConfig:
    data = yaml.safe_load(Path(path).read_text())
    return AppConfig.model_validate(data)
