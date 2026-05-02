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


class AppConfig(BaseModel):
    opend: OpenDConfig
    database: DatabaseConfig
    scheduler: SchedulerConfig
    lark: LarkConfig
    signals: SignalsConfig
    logging: LoggingConfig


def load_watchlist(path: str | Path) -> WatchlistConfig:
    data = yaml.safe_load(Path(path).read_text())
    return WatchlistConfig.model_validate(data)


def load_settings(path: str | Path) -> AppConfig:
    data = yaml.safe_load(Path(path).read_text())
    return AppConfig.model_validate(data)
