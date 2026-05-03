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
    """LLM-driven strategy. Skeleton only — the actual LLMStrategy lands in C2.

    Kept here so settings.yaml is forward-compatible: users can author
    these blocks today and they'll start being honored when C2 lands.
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

    max_position_per_symbol: int = 200
    min_trade_size: int = 10
    min_confidence: float = 0.6
    fallback_on_error: Literal["rule", "hold"] = "rule"

    kline_window: int = 200
    news_window_minutes: int = 30
    news_top_k: int = 3

    cache_seconds: int = 300
    max_concurrent: int = 5


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
