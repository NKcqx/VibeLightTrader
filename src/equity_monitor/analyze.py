"""Ad-hoc analysis pipeline — user-triggered, not signal-driven.

`run_intraday_check` only consults the LLM when a price/indicator signal
fires; that's the right behaviour for a token-budgeted cron loop. But
when the user (or `equity-monitor analyze` CLI) explicitly asks "what's
your view on NVDA right now?", we want a decision regardless of whether
RSI breached 70 in the last hour.

This module assembles the same data the scheduler would (latest 60m
bar + indicators, last ~90 days of quotes, current position) and
renders the medium-term prompt. It returns a structured `AnalysisResult`
per symbol — the CLI decides whether to print it, push it to Lark, or
turn it into an executed paper order.

Design: keep this module thin and database-only. It does NOT call OpenD
for live quotes; the most recent persisted quote is good enough for
medium-term decisions and avoids the 2-3s latency of a fresh quote
roundtrip per symbol. CLI users who want a fresh quote should `equity-
monitor backfill --days 1` before analyzing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from equity_monitor.llm.client import (
    LLMClient,
    LLMError,
    LLMParseError,
    LLMTimeoutError,
)
from equity_monitor.llm.factory import build_llm_client
from equity_monitor.llm.prompt import (
    DEFAULT_SYSTEM_PROMPT,
    DEFAULT_USER_TEMPLATE,
    ParsedDecision,
    parse_decision,
    render_user_prompt,
)
from equity_monitor.models import Indicator, Position, Quote, Symbol


@dataclass(frozen=True)
class AnalysisResult:
    """One symbol's analyze output. Always populated even on failure;
    the `error` field tells you whether to trust `decision`.
    """

    code: str
    name: str
    last_close: float
    indicators: dict[str, float | None]
    position_qty: int
    avg_cost: float
    realized_pnl: float
    decision: ParsedDecision | None
    raw_text: str
    latency_ms: int
    error: str | None = None


def analyze_symbols(
    session: Session,
    *,
    cfg: Any,
    codes: list[str],
    profile_overrides: dict[str, Any] | None = None,
    quotes_lookback: int = 630,
) -> list[AnalysisResult]:
    """Run an LLM analysis for each code; never raises.

    Args:
        session: open SQLAlchemy session (caller manages txn).
        cfg: AppConfig — provides `trader.strategy.llm` (LLM client knobs)
            + `trader.investment_profile` (medium-term framing).
        codes: list of symbol codes (e.g. ['US.NVDA', 'US.MSFT']). Unknown
            codes get an `error="symbol not in DB"` result rather than
            being silently skipped — keeps the CLI honest.
        profile_overrides: optional dict to merge into the configured
            profile before rendering. Useful for `--budget 30000` etc.
        quotes_lookback: how many recent quote rows to pull (informs the
            return-summary in the prompt).

    Returns:
        One `AnalysisResult` per code, in the order requested.
    """
    profile = _materialised_profile(cfg, profile_overrides)
    llm_cfg = cfg.trader.strategy.llm
    client = _make_client(llm_cfg)

    results: list[AnalysisResult] = []
    for code in codes:
        results.append(
            _analyze_one(
                session,
                code=code,
                profile=profile,
                llm_cfg=llm_cfg,
                client=client,
                quotes_lookback=quotes_lookback,
            )
        )
    return results


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _materialised_profile(cfg: Any, overrides: dict[str, Any] | None) -> Any:
    """Apply CLI overrides on top of the configured InvestmentProfile.

    Returns a *copy* — the runtime cfg object stays clean. We accept the
    pydantic model and copy it so the prompt template's attribute access
    still works (no need to convert to dict).
    """
    base = cfg.trader.investment_profile
    if not overrides:
        return base
    # pydantic v2: model_copy(update=...) keeps validators in play.
    return base.model_copy(update=overrides)


def _make_client(llm_cfg: Any) -> LLMClient:
    """Build the LLM client from settings. Mirrors `_build_llm_strategy`
    but stays here to keep the CLI from pulling in the full strategy
    registration machinery (which would side-effect register HITL etc.).
    """
    workspace = llm_cfg.cursor_agent_workspace
    if llm_cfg.provider == "cursor-agent" and not workspace:
        workspace = str(Path.cwd().resolve())
    return build_llm_client(
        provider=llm_cfg.provider,
        model=llm_cfg.model,
        api_key_env=llm_cfg.api_key_env,
        base_url=llm_cfg.base_url,
        workspace=workspace,
        cursor_agent_binary=llm_cfg.cursor_agent_binary,
        cursor_agent_extra_flags=tuple(llm_cfg.cursor_agent_extra_flags),
    )


def _analyze_one(
    session: Session,
    *,
    code: str,
    profile: Any,
    llm_cfg: Any,
    client: LLMClient,
    quotes_lookback: int,
) -> AnalysisResult:
    sym = (
        session.query(Symbol)
        .filter(Symbol.code == code, Symbol.is_active.is_(True))
        .one_or_none()
    )
    if sym is None:
        return AnalysisResult(
            code=code, name=code, last_close=0.0,
            indicators={}, position_qty=0, avg_cost=0.0, realized_pnl=0.0,
            decision=None, raw_text="", latency_ms=0,
            error=f"symbol not in DB (run `equity-monitor watchlist sync` first)",
        )

    ind_row = (
        session.query(Indicator)
        .filter(Indicator.symbol_id == sym.id)
        .order_by(Indicator.ts.desc())
        .first()
    )
    indicators: dict[str, float | None] = {
        "rsi_14": getattr(ind_row, "rsi_14", None) if ind_row else None,
        "macd": getattr(ind_row, "macd", None) if ind_row else None,
        "macd_signal": getattr(ind_row, "macd_signal", None) if ind_row else None,
        "macd_hist": getattr(ind_row, "macd_hist", None) if ind_row else None,
        "boll_upper": getattr(ind_row, "boll_upper", None) if ind_row else None,
        "boll_mid": getattr(ind_row, "boll_mid", None) if ind_row else None,
        "boll_lower": getattr(ind_row, "boll_lower", None) if ind_row else None,
    }

    quote_rows = (
        session.query(Quote)
        .filter(Quote.symbol_id == sym.id)
        .order_by(Quote.ts.desc())
        .limit(quotes_lookback)
        .all()
    )
    last_close = float(quote_rows[0].close) if quote_rows else 0.0
    intraday_return: float | None = None
    last_30_bar_return: float | None = None
    if len(quote_rows) >= 2:
        # `quote_rows[1]` is the previous bar (DESC order)
        prev = float(quote_rows[1].close)
        if prev:
            intraday_return = (last_close - prev) / prev
    if len(quote_rows) >= 30:
        prev_30 = float(quote_rows[29].close)
        if prev_30:
            last_30_bar_return = (last_close - prev_30) / prev_30

    pos = (
        session.query(Position)
        .filter(Position.symbol_id == sym.id)
        .one_or_none()
    )
    position_qty = int(pos.qty) if pos else 0
    avg_cost = float(pos.avg_cost) if pos else 0.0
    realized_pnl = float(pos.realized_pnl or 0.0) if pos else 0.0

    # Synthesize a minimal "live snapshot" object exposing just the
    # attributes the prompt template reads. Avoids round-tripping OpenD.
    class _LiveSnapshot:
        def __init__(self, last_price: float) -> None:
            self.last_price = last_price

    snapshot = _LiveSnapshot(last_close) if last_close else None

    user_prompt = render_user_prompt(
        code=code,
        snapshot=snapshot,
        position_qty=position_qty,
        avg_cost=avg_cost,
        realized_pnl=realized_pnl,
        intraday_return=intraday_return,
        last_30_bar_return=last_30_bar_return,
        indicators=indicators if ind_row else None,
        signals=[],  # ad-hoc analyze: no triggered signals by definition
        max_position=int(profile.budget_per_symbol_usd / max(last_close, 1)),
        min_trade_size=llm_cfg.min_trade_size,
        min_confidence=llm_cfg.min_confidence,
        profile=profile,
        template=DEFAULT_USER_TEMPLATE,
    )

    messages = [
        {"role": "system", "content": DEFAULT_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    t0 = time.monotonic()
    try:
        response = client.chat(
            messages,
            max_tokens=llm_cfg.max_tokens,
            temperature=llm_cfg.temperature,
            timeout_s=float(llm_cfg.timeout_s),
        )
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        decision = parse_decision(response.text)
        return AnalysisResult(
            code=code, name=sym.name or code, last_close=last_close,
            indicators=indicators, position_qty=position_qty,
            avg_cost=avg_cost, realized_pnl=realized_pnl,
            decision=decision, raw_text=response.text,
            latency_ms=elapsed_ms, error=None,
        )
    except (LLMTimeoutError, LLMParseError, LLMError) as e:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        return AnalysisResult(
            code=code, name=sym.name or code, last_close=last_close,
            indicators=indicators, position_qty=position_qty,
            avg_cost=avg_cost, realized_pnl=realized_pnl,
            decision=None, raw_text="",
            latency_ms=elapsed_ms,
            error=f"{type(e).__name__}: {e}",
        )
