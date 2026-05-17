from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

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
    """Lark/Feishu OpenAPI transport.

    The runtime authenticates as a Custom App (open.feishu.cn → 应用配置 →
    凭证与基础信息). ``app_id`` is committed in YAML; ``app_secret`` is read
    from the env var named in ``app_secret_env`` so secrets stay out of
    git. Lark notifications are *opt-in*: leaving ``app_id`` as ``None``
    keeps the rest of the pipeline running (DB / OpenD / LLM / paper
    trades) but no cards will be pushed.
    """

    app_id: str | None = None
    """Custom App ``app_id`` (e.g. ``cli_a8e94fbcd2f8d...``). When ``None``,
    Lark transport is disabled and no messages are sent."""

    app_secret_env: str = "LARK_APP_SECRET"
    """Env var holding the Custom App ``app_secret``. Read at startup; the
    process refuses to send Lark messages if ``app_id`` is set but the env
    var is empty."""

    base_url: str = "https://open.feishu.cn"
    """OpenAPI host. China users keep the default ``open.feishu.cn``;
    Lark international: ``https://open.larksuite.com``."""

    receiver: LarkReceiver
    """Default recipient for cron-pushed cards (intraday / morning /
    closing briefs). Two-way listener replies always go back to the
    sender — this only governs unsolicited pushes."""

    # ---- deprecated knobs (kept so legacy YAML still loads) -----------

    cli_path: str = "lark-cli"
    """DEPRECATED. Reserved for backwards-compat YAML loading only — no
    longer consulted at runtime now that the HTTP client supersedes the
    ``lark-cli`` subprocess shim. Will be removed in a future release."""

    identity: Literal["bot", "user"] = "bot"
    """DEPRECATED. The HTTP transport always sends as the Custom App
    bot identity; the legacy ``user`` identity (sending as a logged-in
    human via ``lark-cli auth login``) is no longer supported."""


class SignalsConfig(BaseModel):
    rsi_overbought: float = 70
    rsi_oversold: float = 30
    bollinger_period: int = 20
    bollinger_std: float = 2
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    dedupe_window_minutes: int = 60


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: str | None = None


class FundamentalsConfig(BaseModel):
    """Wall-Street consensus / news / earnings feed for LLM strategies.

    Data is snapshotted from yfinance via
    ``scripts/refresh_fundamentals_fixtures.py`` and read at runtime from a
    local fixture directory. The runtime never touches yfinance directly —
    keep ``source: fixture`` and refresh the fixture deliberately.
    """

    source: Literal["fixture", "none"] = "fixture"
    """``"fixture"`` reads ``src/vibe_trader/data/fixtures/fundamentals/raw/``;
    ``"none"`` disables fundamentals (LLM won't see consensus/news)."""

    fixture_dir: str | None = None
    """Override the default fixture directory. None = package default."""

    max_rating_changes: int = 20
    """Cap on how many recent rating changes are loaded per symbol."""

    max_news: int = 10
    """Cap on how many recent news headlines are loaded per symbol."""

    prompt_max_changes: int = 5
    """Cap on rating changes rendered into the LLM prompt (UI density)."""

    prompt_max_news: int = 5
    """Cap on news headlines rendered into the LLM prompt."""


class StrategyRuleConfig(BaseModel):
    """Knobs for the hard-coded rule strategy (a.k.a. strategy_lite)."""

    max_position_per_symbol: int = 200
    critical_size: int = 100
    warn_size: int = 50
    rsi_extreme: float = 30.0


class StrategyLLMConfig(BaseModel):
    """LLM-driven strategy. Implemented in `signals/strategy_llm.py:LLMStrategy`.

    Switch from rule → llm by setting `trader.strategy.type: llm` AND
    pointing `provider` at the right backend:

      - provider=anthropic       → needs ANTHROPIC_API_KEY env var
      - provider=openai_compat   → needs whatever `api_key_env` names
                                   (OPENAI_API_KEY, DEEPSEEK_API_KEY,
                                    ARK_API_KEY, OPENROUTER_API_KEY, ...)
      - provider=cursor-agent    → no API key. Spawns the locally-installed
                                   `cursor-agent` CLI; consumes the user's
                                   IDE Pro/Max subscription quota. Run
                                   `cursor-agent login` once first.

    Local Ollama / vLLM servers don't need a key — leave `api_key_env`
    empty (the Authorization header is then omitted).
    """

    provider: Literal["anthropic", "openai_compat", "cursor-agent"] = "anthropic"
    model: str = "claude-3-5-sonnet-20241022"
    """For provider=cursor-agent, common values: 'sonnet-4',
    'sonnet-4-thinking', 'gpt-5', 'auto'. Leave empty to inherit the
    user's account default."""

    api_key_env: str = "ANTHROPIC_API_KEY"
    """Ignored when provider=cursor-agent (auth is via IDE login)."""

    base_url: str | None = None
    """Required for `openai_compat`; ignored by other providers."""

    cursor_agent_binary: str = "cursor-agent"
    """Path or PATH-resolved name of the cursor-agent executable.
    Only used when provider=cursor-agent."""

    cursor_agent_workspace: str | None = None
    """Working directory the cursor-agent treats as cwd for its file tools.
    None → repo root (auto-detected at strategy build time). Only used
    when provider=cursor-agent."""

    cursor_agent_extra_flags: list[str] = Field(default_factory=list)
    """Appended to every `cursor-agent` invocation. Use to pass e.g.
    ['--mode', 'plan'] for read-only runs, or ['--force'] to allow
    Shell/Edit tools (NOT recommended for the trading loop)."""

    max_tokens: int = 512
    temperature: float = 0.0
    timeout_s: int = 30
    """For cursor-agent provider: bump this to 180+ — the CLI typically
    takes 30-60s per call (it actually runs an agent loop, not a single
    chat request)."""

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
    """How many K-line bars to feed the StrategyContext per tick."""

    cache_seconds: int = 300
    max_concurrent: int = 5
    """`max_concurrent` is reserved (today the strategy runs serially per
    symbol). Will matter once we parallelise LLM calls."""


class StrategyEnsembleConfig(BaseModel):
    """Ensemble strategy. Skeleton only — actual implementation in C3."""

    strategies: list[str] = Field(default_factory=lambda: ["rule"])
    voting: Literal["majority", "weighted", "unanimous"] = "weighted"
    weights: dict[str, float] = Field(default_factory=lambda: {"rule": 1.0})


class StrategyHITLConfig(BaseModel):
    """Human-in-the-loop strategy.

    Each event triggers writing a decision packet to `var_dir`. The user
    pastes the markdown prompt into Cursor / Claude.app where another
    instance of the same model decides; the user then submits the JSON
    decision via `vibe-trader decide submit`. No API key needed —
    drives entirely off whatever LLM-subscription IDE the user already
    has.
    """

    var_dir: str = "var/decisions"
    """Where pending/submitted/executed/cancelled packets live."""

    repo_root: str | None = None
    """Absolute path used in the packet's submit-command. None →
    rendered using cwd at packet-creation time."""

    max_position_per_symbol: int = 200
    min_trade_size: int = 10
    min_confidence: float = 0.6
    """Same hard-constraint knobs as the LLM strategy. Mirrored into
    every packet so the user / receiver knows the rails."""


class StrategyConfig(BaseModel):
    """Active-strategy selector + per-strategy knobs.

    Only the sub-block matching `type` is consumed; the others sit dormant
    and forward-compatible.
    """

    type: Literal["rule", "llm", "hitl", "ensemble"] = "rule"
    rule: StrategyRuleConfig = Field(default_factory=StrategyRuleConfig)
    llm: StrategyLLMConfig = Field(default_factory=StrategyLLMConfig)
    hitl: StrategyHITLConfig = Field(default_factory=StrategyHITLConfig)
    ensemble: StrategyEnsembleConfig = Field(default_factory=StrategyEnsembleConfig)


class InvestmentProfileConfig(BaseModel):
    """User's investment thesis — strategy-agnostic.

    Fed verbatim into the LLM prompt for every decision, and used by
    rule-based safety nets (min_holding_days, hard_stop_pct, etc.). Keeps
    'who I am as an investor' decoupled from 'which strategy am I using
    today'. Default values target a 3-6 month tech-growth playbook that
    fits a $50k-per-symbol budget with 20% drawdown tolerance.

    Set `enabled: false` to fall back to legacy short-term framing (pre
    M-series). Pre-canned profiles live in `docs/mid-term-investing.md`.
    """

    enabled: bool = True

    # ---- Horizon & style ------------------------------------------------
    horizon_months_min: int = 3
    horizon_months_max: int = 6
    style: Literal["growth", "value", "blend", "income", "speculative"] = "growth"

    theme: str = "AI-infrastructure & cloud-incumbent mid-term swing"
    """Free-text thesis fed verbatim to the LLM. Keep <200 chars."""

    # ---- Capital & risk -------------------------------------------------
    budget_per_symbol_usd: float = 50_000.0
    """Target dollar exposure per symbol when fully built up."""

    drawdown_tolerance_pct: float = 20.0
    """Max acceptable per-symbol drawdown from average cost."""

    max_concentration_pct: float = 60.0
    """Single-symbol cap as % of total deployed capital."""

    cash_reserve_pct: float = 10.0
    """Always-uninvested cash buffer. Informational today; honored by
    the portfolio-aware sizer in M-3 (reserved)."""

    # ---- Entry & sizing -------------------------------------------------
    initial_entry_pct: float = 40.0
    """First buy is N% of `budget_per_symbol_usd`. Rest reserved for
    add-ons. Set to 100 for one-shot sizing."""

    max_batches: int = 3
    """Maximum number of accumulating buys. After this, BUY is declined
    regardless of LLM signal."""

    add_on_dip_pct: float = 5.0
    """Add-on buy requires price to be at least N% below the most-recent
    fill / avg-cost since the last buy."""

    add_cooldown_days: int = 5
    """Minimum days between add-on buys (avoid panic averaging-down)."""

    prefer_dip_buy: bool = True
    """Hint to LLM: bias entries toward technical pullbacks (RSI<40,
    BB lower band, etc.)."""

    earnings_blackout_days: int = 3
    """Don't initiate new positions within N days BEFORE earnings.
    Reserved — needs an earnings-calendar data source."""

    # ---- Exit -----------------------------------------------------------
    take_profit_pct: float = 30.0
    """Trim trigger: when unrealized return exceeds N%, the LLM is
    nudged toward partial profit. 0 disables."""

    take_profit_trim_pct: float = 50.0
    """Fraction of position to skim when `take_profit_pct` triggers
    (default 50%, i.e. half-position). 100 = full exit."""

    hard_stop_pct: float = 20.0
    """Hard SELL when unrealized loss exceeds N% from avg cost. Enforced
    regardless of LLM. Sane default = `drawdown_tolerance_pct`."""

    trailing_stop_pct: float | None = None
    """Sell when price drops N% from highest close since entry.
    None = disabled. Reserved — needs running-high tracker."""

    min_holding_days: int = 30
    """Block voluntary SELL within N days of buy (hard_stop bypasses).
    Prevents LLM-noise churning. Set to 0 for short-term setups."""

    # ---- LLM-prompt helpers --------------------------------------------
    rebalance_cadence_days: int = 30
    """Re-evaluate the full thesis every N days (long-form review prompt
    instead of the regular tick prompt). Reserved."""

    valuation_ceiling_pe: float | None = None
    """If set, LLM should decline BUY when forward P/E exceeds this.
    None = disabled. Reserved — needs fundamentals source."""

    @field_validator("horizon_months_max")
    @classmethod
    def _horizon_order(cls, v: int, info: Any) -> int:  # type: ignore[name-defined]
        lo = info.data.get("horizon_months_min", 0)
        if v < lo:
            raise ValueError(
                f"horizon_months_max ({v}) must be >= horizon_months_min ({lo})"
            )
        return v


class TraderConfig(BaseModel):
    """Paper-trading auto-execution settings."""

    auto_execute: bool = True
    """If True, run_intraday_check executes BUY/SELL suggestions through
    the paper broker and persists Trade/Position rows automatically. If
    False, suggestions are only displayed in the alert card; user runs
    `vibe-trader trade confirm <signal_id>` to execute manually.
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

    investment_profile: InvestmentProfileConfig = Field(
        default_factory=InvestmentProfileConfig
    )
    """Cross-strategy investor profile (horizon, budget, drawdown, exits).
    Fed into the LLM prompt; informs rule-side safety nets. See
    `docs/mid-term-investing.md` for ready-made profiles."""


class AppConfig(BaseModel):
    opend: OpenDConfig
    database: DatabaseConfig
    scheduler: SchedulerConfig
    lark: LarkConfig
    signals: SignalsConfig
    logging: LoggingConfig
    trader: TraderConfig = Field(default_factory=TraderConfig)
    fundamentals: FundamentalsConfig = Field(default_factory=FundamentalsConfig)


def load_watchlist(path: str | Path) -> WatchlistConfig:
    data = yaml.safe_load(Path(path).read_text())
    return WatchlistConfig.model_validate(data)


def load_settings(path: str | Path) -> AppConfig:
    data = yaml.safe_load(Path(path).read_text())
    return AppConfig.model_validate(data)
