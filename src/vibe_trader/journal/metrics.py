"""Decision-quality metrics for the per-symbol overview block.

Two windows by default:
  - "近 7 天":  decisions within the last 7d, evaluated 1 trading-day later
  - "近 30 天": decisions within the last 30d, evaluated 7 trading-days later

A "hit" means the price moved in the same direction the decision bet
on:
  - BUY hit  ⟺ later_close >= entry_close
  - SELL hit ⟺ later_close <= entry_close
HOLD decisions are NOT counted (HOLD has no directional bet).

Data sources (no schema changes):
  - data/llm_decisions.jsonl — append-only NDJSON written by LLMStrategy.
    Each line records (ts_unix, code, decision={action,qty,reason},
    fallback_used, ...). We read decision.action.
  - quotes table — entry price = the most-recent quote at-or-before the
    decision ts; later price = the most-recent quote at-or-before
    (decision_ts + eval_after). When either is missing the decision is
    counted as "pending" and excluded from the rate.

Why use the audit log for decisions instead of `signals.suggested_action`?
The audit log is the canonical record of what the LLM (or fallback) said
on each tick — including HOLDs and decisions whose tick produced no
SignalRow (e.g. degenerate edge cases). It also predates the journal,
so users have a few weeks of history available before journal files
even exist.

Performance: a single tick reads at most ~tail-of-file lines per code,
once. We don't index — `data/llm_decisions.jsonl` is normally < 5 MB
and the JSON parse is well under 50 ms for typical sizes. A future
optimisation could compute incrementally if this becomes hot.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import structlog
from sqlalchemy import desc
from sqlalchemy.orm import sessionmaker

from vibe_trader.db import session_scope
from vibe_trader.models import Quote, Symbol

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class HitRateStats:
    """Aggregated decision-quality stat for one (window, eval-after) combo.

    `pending` is decisions whose eval window hasn't yet elapsed (or where
    we don't have a price quote far enough out). They are NOT counted
    in `hit_rate`'s denominator.
    """

    window_label: str  # 用于显示 ("近 7 天")
    decision_window_days: int
    eval_after_days: int

    decisions_total: int  # all decisions (incl. HOLD) in window
    actionable_total: int  # BUY+SELL in window
    evaluated: int  # actionable AND eval window passed AND prices found
    hits: int
    misses: int
    pending: int  # actionable but eval window not yet elapsed

    @property
    def hit_rate(self) -> float | None:
        return self.hits / self.evaluated if self.evaluated > 0 else None


@dataclass(frozen=True)
class _DecisionRow:
    """Minimal projection of one audit-log line for the metrics computation."""

    ts: datetime  # tz-aware UTC
    action: str  # "BUY" / "SELL" / "HOLD"
    fallback_used: bool


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def compute_hit_rates(
    *,
    audit_log_path: Path,
    factory: sessionmaker,
    code: str,
    cutoff: datetime,
) -> list[HitRateStats]:
    """Return the two default hit-rate windows for `code`.

    `cutoff` is "now"; the windows are [cutoff - <window>, cutoff).
    """
    return [
        _compute_one_window(
            audit_log_path=audit_log_path,
            factory=factory,
            code=code,
            cutoff=cutoff,
            window_label="近 7 天",
            decision_window_days=7,
            eval_after_days=1,
        ),
        _compute_one_window(
            audit_log_path=audit_log_path,
            factory=factory,
            code=code,
            cutoff=cutoff,
            window_label="近 30 天",
            decision_window_days=30,
            eval_after_days=7,
        ),
    ]


def render_hit_rate_lines(stats: Iterable[HitRateStats]) -> list[str]:
    """Format each stat as one Markdown bullet line.

    Returns an empty list when there are no actionable decisions yet —
    the caller can decide whether to omit the section entirely.
    """
    lines: list[str] = []
    for s in stats:
        if s.actionable_total == 0:
            if s.decisions_total == 0:
                detail = "窗口内尚无任何决策"
            else:
                detail = f"窗口内仅有 {s.decisions_total} 次 HOLD"
            lines.append(
                f"- **{s.window_label}决策胜率**：— ({detail})"
            )
            continue
        if s.evaluated == 0:
            lines.append(
                f"- **{s.window_label}决策胜率**：— "
                f"({s.actionable_total} 个 BUY/SELL 决策，"
                f"评估期未到 / 数据不足)"
            )
            continue
        rate = s.hit_rate or 0.0
        lines.append(
            f"- **{s.window_label}决策胜率**：{rate * 100:.0f}% "
            f"({s.hits}/{s.evaluated}) · "
            f"hold={s.eval_after_days}d · pending {s.pending}"
        )
    return lines


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _compute_one_window(
    *,
    audit_log_path: Path,
    factory: sessionmaker,
    code: str,
    cutoff: datetime,
    window_label: str,
    decision_window_days: int,
    eval_after_days: int,
) -> HitRateStats:
    cutoff_utc = _ensure_utc(cutoff)
    window_start = cutoff_utc - timedelta(days=decision_window_days)

    decisions = _load_decisions(
        audit_log_path=audit_log_path, code=code, since=window_start, until=cutoff_utc
    )

    actionable = [d for d in decisions if d.action in ("BUY", "SELL")]
    evaluated = 0
    hits = 0
    misses = 0
    pending = 0

    if actionable:
        with session_scope(factory) as session:
            sym = session.query(Symbol).filter(Symbol.code == code).one_or_none()
            sym_id = sym.id if sym is not None else None

            for d in actionable:
                eval_ts = d.ts + timedelta(days=eval_after_days)
                if eval_ts > cutoff_utc:
                    pending += 1
                    continue
                if sym_id is None:
                    pending += 1  # no quotes available at all → can't evaluate
                    continue

                entry_price = _last_close_at_or_before(session, sym_id, d.ts)
                later_price = _last_close_at_or_before(session, sym_id, eval_ts)
                if entry_price is None or later_price is None:
                    pending += 1
                    continue

                evaluated += 1
                if d.action == "BUY":
                    if later_price >= entry_price:
                        hits += 1
                    else:
                        misses += 1
                else:  # SELL
                    if later_price <= entry_price:
                        hits += 1
                    else:
                        misses += 1

    return HitRateStats(
        window_label=window_label,
        decision_window_days=decision_window_days,
        eval_after_days=eval_after_days,
        decisions_total=len(decisions),
        actionable_total=len(actionable),
        evaluated=evaluated,
        hits=hits,
        misses=misses,
        pending=pending,
    )


def _load_decisions(
    *,
    audit_log_path: Path,
    code: str,
    since: datetime,
    until: datetime,
) -> list[_DecisionRow]:
    """Stream the NDJSON audit log; project rows for `code` in [since, until).

    Tolerates missing fields and bad lines — the audit log is best-effort
    and we'd rather show "0 decisions" than crash an overview rebuild.
    """
    if not audit_log_path.exists():
        return []

    out: list[_DecisionRow] = []
    try:
        with audit_log_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("code") != code:
                    continue
                ts_unix = row.get("ts_unix")
                if not isinstance(ts_unix, (int, float)):
                    continue
                ts = datetime.fromtimestamp(ts_unix, tz=timezone.utc)
                if not (since <= ts < until):
                    continue
                decision = row.get("decision") or {}
                action = decision.get("action")
                if action not in ("BUY", "SELL", "HOLD"):
                    continue
                out.append(
                    _DecisionRow(
                        ts=ts,
                        action=action,
                        fallback_used=bool(row.get("fallback_used")),
                    )
                )
    except OSError as e:  # pragma: no cover — disk/permissions edge cases
        log.warning(
            "metrics.audit_read_failed", path=str(audit_log_path), error=repr(e)
        )
        return []
    return out


def _last_close_at_or_before(session, symbol_id: int, ts: datetime) -> float | None:
    """Most-recent Quote.close at-or-before `ts` for `symbol_id`.

    The Quote.ts column is naive UTC in the schema; we strip tzinfo
    on `ts` to compare like-for-like. Using `<=` so a quote stored
    EXACTLY at the decision tick (rare but possible — both ride the
    same scheduler) counts as the entry price.
    """
    ts_naive = ts.replace(tzinfo=None) if ts.tzinfo is not None else ts
    q = (
        session.query(Quote.close)
        .filter(Quote.symbol_id == symbol_id, Quote.ts <= ts_naive)
        .order_by(desc(Quote.ts))
        .first()
    )
    return float(q[0]) if q is not None else None


def _ensure_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
