"""Fundamentals data layer.

This module exposes a small, stable schema for the *fundamental* signals we
feed into the LLM trading strategy:

  - Wall Street consensus (target prices, recommendation key, # analysts)
  - Recent rating changes (upgrades / downgrades / target-price revisions)
  - Recent news headlines (title / summary / publisher / pubDate)
  - Earnings calendar (next earnings date, consensus EPS / revenue)

The data originates from ``yfinance`` but the project deliberately avoids
hitting yfinance over the network in normal operation. Instead, a one-shot
script (``scripts/refresh_fundamentals_fixtures.py``) snapshots the raw
responses into ``src/vibe_trader/data/fixtures/fundamentals/raw/`` and the
runtime always reads those snapshots via :class:`FixtureFundamentalsClient`.

Why fixture-first?

  1. Anti-scrape resilience — we control how often (if ever) we touch yfinance.
  2. Reproducibility — backtests and unit tests use the same data each run.
  3. Easier iteration — schema tweaks reparse the snapshot, no network needed.

To refresh the fixture (rare):

    python scripts/refresh_fundamentals_fixtures.py NVDA MSFT

Then re-import here; nothing else changes.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class AnalystConsensus(BaseModel):
    """Headline Wall Street consensus for a symbol at fixture-fetch time."""

    current_price: float | None = None
    target_mean: float | None = None
    target_high: float | None = None
    target_low: float | None = None
    target_median: float | None = None
    recommendation_key: str | None = None
    """e.g. ``"strong_buy"`` / ``"buy"`` / ``"hold"`` / ``"sell"`` / ``"strong_sell"``."""
    recommendation_mean: float | None = None
    """1.0 (strong buy) - 5.0 (strong sell)."""
    num_analysts: int | None = None

    rating_distribution: dict[str, int] = Field(default_factory=dict)
    """Most-recent period bucket counts: ``{"strongBuy": 9, "buy": 48, ...}``."""

    @property
    def upside_pct(self) -> float | None:
        """Percentage upside from current to mean target. None if missing."""
        if self.current_price and self.target_mean:
            return (self.target_mean - self.current_price) / self.current_price * 100
        return None


class RatingChange(BaseModel):
    """One row from yfinance ``upgrades_downgrades``."""

    grade_date: datetime
    firm: str
    to_grade: str
    from_grade: str | None = None
    action: str | None = None
    """yfinance raw action: ``"reit"`` / ``"main"`` / ``"up"`` / ``"down"`` / ``"init"`` ..."""
    price_target_action: str | None = None
    current_price_target: float | None = None
    prior_price_target: float | None = None


class NewsItem(BaseModel):
    """One news article from yfinance ``Ticker.news``."""

    title: str
    summary: str | None = None
    publisher: str | None = None
    pub_date: datetime | None = None
    url: str | None = None


class EarningsCalendar(BaseModel):
    """Next earnings event from yfinance ``Ticker.calendar``."""

    earnings_date: date | None = None
    """The next reporting date (or the most-recent past one if not yet reported)."""
    earnings_high: float | None = None
    earnings_low: float | None = None
    earnings_avg: float | None = None
    revenue_high: float | None = None
    revenue_low: float | None = None
    revenue_avg: float | None = None
    dividend_date: date | None = None
    ex_dividend_date: date | None = None


class Fundamentals(BaseModel):
    """Full fundamentals bundle for one symbol.

    This is the top-level value :class:`FundamentalsClient.fetch` returns. All
    nested fields are optional in case the upstream snapshot is partial.
    """

    code: str
    """Trading code, e.g. ``"US.NVDA"``."""

    fetched_at: datetime
    """When the underlying yfinance fixture was captured (UTC)."""

    consensus: AnalystConsensus = Field(default_factory=AnalystConsensus)
    recent_rating_changes: list[RatingChange] = Field(default_factory=list)
    """Newest first, capped to the last ~20 entries to keep prompts compact."""

    rating_history: list[dict[str, Any]] = Field(default_factory=list)
    """Per-period (0m, -1m, -2m, -3m) bucket counts; useful for trend lines."""

    news: list[NewsItem] = Field(default_factory=list)
    """Newest first, capped per ``max_news`` at parse time."""

    earnings: EarningsCalendar = Field(default_factory=EarningsCalendar)


# ---------------------------------------------------------------------------
# Parser: raw yfinance JSON -> Fundamentals
# ---------------------------------------------------------------------------


def _safe_get(d: Any, key: str, default: Any = None) -> Any:
    if isinstance(d, dict):
        return d.get(key, default)
    return default


def _parse_dt(value: Any) -> datetime | None:
    """Parse the various date/time string shapes yfinance returns."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        v = value.strip()
        if not v:
            return None
        # Common ISO-8601 variants; tolerate trailing Z / missing tz.
        try:
            if v.endswith("Z"):
                return datetime.fromisoformat(v[:-1]).replace(tzinfo=timezone.utc)
            return datetime.fromisoformat(v)
        except ValueError:
            return None
    return None


def _parse_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, list) and value:
        # ``calendar['Earnings Date']`` is a list, e.g. ``["2026-05-21"]``.
        return _parse_date(value[0])
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _parse_consensus(info: dict[str, Any], recommendations: list[dict[str, Any]]) -> AnalystConsensus:
    info = info or {}
    cons = AnalystConsensus(
        current_price=info.get("currentPrice"),
        target_mean=info.get("targetMeanPrice"),
        target_high=info.get("targetHighPrice"),
        target_low=info.get("targetLowPrice"),
        target_median=info.get("targetMedianPrice"),
        recommendation_key=info.get("recommendationKey"),
        recommendation_mean=info.get("recommendationMean"),
        num_analysts=info.get("numberOfAnalystOpinions"),
    )
    # Latest-period distribution is recommendations[0] (period == "0m").
    if recommendations:
        head = recommendations[0]
        cons.rating_distribution = {
            "strongBuy": int(head.get("strongBuy", 0) or 0),
            "buy": int(head.get("buy", 0) or 0),
            "hold": int(head.get("hold", 0) or 0),
            "sell": int(head.get("sell", 0) or 0),
            "strongSell": int(head.get("strongSell", 0) or 0),
        }
    return cons


def _parse_rating_changes(rows: list[dict[str, Any]] | None, max_rows: int) -> list[RatingChange]:
    if not rows:
        return []
    parsed: list[RatingChange] = []
    for row in rows[:max_rows]:
        ts = _parse_dt(row.get("GradeDate"))
        if ts is None:
            continue
        parsed.append(
            RatingChange(
                grade_date=ts,
                firm=str(row.get("Firm") or "?"),
                to_grade=str(row.get("ToGrade") or "?"),
                from_grade=row.get("FromGrade") or None,
                action=row.get("Action") or None,
                price_target_action=row.get("priceTargetAction") or None,
                current_price_target=row.get("currentPriceTarget"),
                prior_price_target=row.get("priorPriceTarget"),
            )
        )
    return parsed


def _parse_news(rows: list[dict[str, Any]] | None, max_news: int) -> list[NewsItem]:
    """yfinance news shape: ``[{"id": ..., "content": {title, summary, pubDate, provider, canonicalUrl, ...}}]``."""
    if not rows:
        return []
    items: list[NewsItem] = []
    for raw in rows[:max_news]:
        content = raw.get("content") if isinstance(raw, dict) else None
        if not isinstance(content, dict):
            continue
        title = content.get("title")
        if not title:
            continue
        provider = _safe_get(content.get("provider"), "displayName")
        # ``canonicalUrl`` is a dict in some payloads, str in others.
        canon = content.get("canonicalUrl")
        if isinstance(canon, dict):
            url = canon.get("url")
        elif isinstance(canon, str):
            url = canon
        else:
            url = content.get("clickThroughUrl") if isinstance(content.get("clickThroughUrl"), str) else None
            if isinstance(content.get("clickThroughUrl"), dict):
                url = content["clickThroughUrl"].get("url")
        items.append(
            NewsItem(
                title=str(title),
                summary=content.get("summary") or content.get("description") or None,
                publisher=provider,
                pub_date=_parse_dt(content.get("pubDate") or content.get("displayTime")),
                url=url,
            )
        )
    return items


def _parse_calendar(cal: dict[str, Any] | None) -> EarningsCalendar:
    cal = cal if isinstance(cal, dict) else {}
    return EarningsCalendar(
        earnings_date=_parse_date(cal.get("Earnings Date")),
        earnings_high=cal.get("Earnings High"),
        earnings_low=cal.get("Earnings Low"),
        earnings_avg=cal.get("Earnings Average"),
        revenue_high=cal.get("Revenue High"),
        revenue_low=cal.get("Revenue Low"),
        revenue_avg=cal.get("Revenue Average"),
        dividend_date=_parse_date(cal.get("Dividend Date")),
        ex_dividend_date=_parse_date(cal.get("Ex-Dividend Date")),
    )


def parse_raw_fundamentals(
    raw: dict[str, Any],
    *,
    max_rating_changes: int = 20,
    max_news: int = 10,
) -> Fundamentals:
    """Convert the raw yfinance snapshot dict into a :class:`Fundamentals`.

    The snapshot dict is what ``scripts/refresh_fundamentals_fixtures.py``
    writes — a single JSON object with ``info``, ``recommendations``,
    ``upgrades_downgrades``, ``news``, ``calendar`` etc.
    """
    code = raw.get("code") or f"US.{raw.get('ticker', '?')}"
    fetched = _parse_dt(raw.get("fetched_at")) or datetime.now(tz=timezone.utc)
    info = raw.get("info") or {}
    recs_raw = raw.get("recommendations") or []
    if not isinstance(recs_raw, list):
        recs_raw = []

    return Fundamentals(
        code=str(code),
        fetched_at=fetched,
        consensus=_parse_consensus(info, recs_raw),
        recent_rating_changes=_parse_rating_changes(
            raw.get("upgrades_downgrades") if isinstance(raw.get("upgrades_downgrades"), list) else None,
            max_rating_changes,
        ),
        rating_history=[
            {
                "period": r.get("period"),
                "strongBuy": int(r.get("strongBuy", 0) or 0),
                "buy": int(r.get("buy", 0) or 0),
                "hold": int(r.get("hold", 0) or 0),
                "sell": int(r.get("sell", 0) or 0),
                "strongSell": int(r.get("strongSell", 0) or 0),
            }
            for r in recs_raw
        ],
        news=_parse_news(raw.get("news") if isinstance(raw.get("news"), list) else None, max_news),
        earnings=_parse_calendar(raw.get("calendar")),
    )


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------


@runtime_checkable
class FundamentalsClient(Protocol):
    """Read-only fundamentals lookup. Returns None when the symbol is unknown."""

    name: str

    def fetch(self, code: str) -> Fundamentals | None: ...


_DEFAULT_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "fundamentals" / "raw"


class FixtureFundamentalsClient:
    """Reads raw yfinance snapshots from a directory of JSON files.

    This is the *production default*. Reads happen at most once per cron tick
    and are I/O-cheap (≈300KB JSON parse).
    """

    name = "fixture"

    def __init__(self, root: Path | str | None = None, *, max_rating_changes: int = 20, max_news: int = 10) -> None:
        self.root = Path(root) if root else _DEFAULT_FIXTURE_DIR
        self.max_rating_changes = max_rating_changes
        self.max_news = max_news

    def _candidate_paths(self, code: str) -> list[Path]:
        paths = [self.root / f"{code}.json"]
        # Tolerate both "US.NVDA.json" and "NVDA.json" naming.
        if "." in code:
            paths.append(self.root / f"{code.split('.', 1)[1]}.json")
        return paths

    def fetch(self, code: str) -> Fundamentals | None:
        for p in self._candidate_paths(code):
            if p.is_file():
                try:
                    raw = json.loads(p.read_text(encoding="utf-8"))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("fundamentals: failed to parse %s: %s", p, exc)
                    return None
                return parse_raw_fundamentals(
                    raw,
                    max_rating_changes=self.max_rating_changes,
                    max_news=self.max_news,
                )
        logger.debug("fundamentals: no fixture for %s under %s", code, self.root)
        return None


class NullFundamentalsClient:
    """No-op client; always returns None. Useful in tests / disable switch."""

    name = "none"

    def fetch(self, code: str) -> Fundamentals | None:  # noqa: ARG002
        return None


def build_fundamentals_client(
    source: str = "fixture",
    *,
    fixture_dir: str | None = None,
    max_rating_changes: int = 20,
    max_news: int = 10,
) -> FundamentalsClient:
    """Factory keyed by ``cfg.fundamentals.source``.

    ``"yfinance"`` is intentionally unsupported here — the live network path
    is *only* reachable through ``scripts/refresh_fundamentals_fixtures.py``.
    """
    src = (source or "fixture").lower()
    if src in {"none", "off", "disabled"}:
        return NullFundamentalsClient()
    if src == "fixture":
        return FixtureFundamentalsClient(
            root=fixture_dir,
            max_rating_changes=max_rating_changes,
            max_news=max_news,
        )
    if src == "yfinance":
        raise ValueError(
            "fundamentals.source='yfinance' is intentionally not wired into the runtime; "
            "use scripts/refresh_fundamentals_fixtures.py to refresh fixtures, then keep "
            "fundamentals.source='fixture'."
        )
    raise ValueError(f"unknown fundamentals.source={source!r}")


# ---------------------------------------------------------------------------
# Prompt helpers (consumed by strategy_llm.py)
# ---------------------------------------------------------------------------


def _summarise_recent_moves(
    fund: Fundamentals,
    *,
    today: date | None,
    window_days: int = 30,
) -> dict[str, Any]:
    """Distill recent rating-change activity into per-window aggregates.

    Returns a dict with:
      - upgrades / downgrades / reiterations: counts in the window
      - pt_raised / pt_cut / pt_unchanged: count by price-target action
      - pt_avg_change_pct: mean % change in current_price_target vs prior
                           (None when no row had both targets)
      - latest: most-recent row date (or None)

    Counts are over ``recent_rating_changes`` (already capped). When
    ``today`` is None we use ``fund.fetched_at`` as the reference date so
    callers get a deterministic answer.
    """
    if not fund.recent_rating_changes:
        return {
            "upgrades": 0, "downgrades": 0, "reiterations": 0,
            "pt_raised": 0, "pt_cut": 0, "pt_unchanged": 0,
            "pt_avg_change_pct": None,
            "latest": None,
        }
    ref = today or fund.fetched_at.date()
    pt_changes: list[float] = []
    out = {
        "upgrades": 0, "downgrades": 0, "reiterations": 0,
        "pt_raised": 0, "pt_cut": 0, "pt_unchanged": 0,
        "latest": fund.recent_rating_changes[0].grade_date.date(),
    }
    for rc in fund.recent_rating_changes:
        d = rc.grade_date.date()
        if (ref - d).days > window_days or (ref - d).days < 0:
            continue
        a = (rc.action or "").lower()
        if a in {"up", "upgrade"}:
            out["upgrades"] += 1
        elif a in {"down", "downgrade"}:
            out["downgrades"] += 1
        else:
            out["reiterations"] += 1
        # Price-target moves: prefer the explicit deltas where available.
        cur, prior = rc.current_price_target, rc.prior_price_target
        if cur is not None and prior is not None and prior > 0:
            pct = (cur - prior) / prior * 100
            if pct > 0.5:
                out["pt_raised"] += 1
            elif pct < -0.5:
                out["pt_cut"] += 1
            else:
                out["pt_unchanged"] += 1
            pt_changes.append(pct)
    out["pt_avg_change_pct"] = (
        sum(pt_changes) / len(pt_changes) if pt_changes else None
    )
    return out


def _format_history_diff(history: list[dict[str, Any]]) -> str | None:
    """Compare the latest period (0m) to the earliest available (-1m, -2m, -3m).

    Returns a short Markdown line, or None when not enough data.
    """
    if len(history) < 2:
        return None
    latest = history[0]
    # Find the oldest period available (largest negative offset).
    oldest = history[-1]
    if not isinstance(latest, dict) or not isinstance(oldest, dict):
        return None
    parts = []
    for k in ("strongBuy", "buy", "hold", "sell", "strongSell"):
        a, b = latest.get(k, 0), oldest.get(k, 0)
        if a != b:
            sign = "+" if a > b else ""
            parts.append(f"{k} {b}→{a} ({sign}{a - b})")
    if not parts:
        return None
    return (
        f"**Rating distribution change (vs {oldest.get('period', '-3m')}):** "
        + ", ".join(parts)
    )


def render_for_prompt(
    fund: Fundamentals | None,
    *,
    max_news: int = 5,
    max_changes: int = 5,
    today: date | None = None,
    blackout_days: int = 3,
    moves_window_days: int = 30,
) -> str:
    """Render Fundamentals as a compact Markdown block for LLM prompts.

    Layout is **change-first**: the model sees recent movement (rating
    upgrades, target revisions, history diff, earnings) before structural
    context (consensus level, full distribution). This nudges decisions
    toward *deltas* rather than re-acting to a stable ``strong_buy`` rating
    every hour.

    Sections (in order):
      1. ``Fundamentals — recent moves (last 30d)``  — counts of
         upgrades / downgrades / target raises / cuts / mean PT change.
      2. ``Recent rating changes`` (most recent N rows, raw stream).
      3. ``Rating distribution change`` (vs the earliest period, e.g. -3m).
      4. ``Wall Street consensus (level)`` — the structural snapshot.
      5. ``Recent news headlines``.
      6. ``Next earnings`` + optional ``WARNING: blackout`` line.

    Returns an empty string when ``fund`` is None so callers can ``str.join``
    without conditionals.

    ``today`` and ``blackout_days`` drive the earnings-blackout warning;
    ``today=None`` skips the warning. ``moves_window_days`` widens or
    narrows the recent-moves aggregation.
    """
    if fund is None:
        return ""
    parts: list[str] = []

    # 1. Recent moves (CHANGE FIRST).
    moves = _summarise_recent_moves(
        fund, today=today, window_days=moves_window_days
    )
    if any(
        moves[k] for k in (
            "upgrades", "downgrades", "reiterations",
            "pt_raised", "pt_cut", "pt_unchanged",
        )
    ):
        pt_avg = moves["pt_avg_change_pct"]
        pt_avg_str = f", mean PT change {pt_avg:+.1f}%" if pt_avg is not None else ""
        parts.append(
            f"**Fundamentals — recent moves (last {moves_window_days}d):** "
            f"{moves['upgrades']} upgrades / "
            f"{moves['downgrades']} downgrades / "
            f"{moves['reiterations']} reiterations; "
            f"PT raised {moves['pt_raised']} / cut {moves['pt_cut']} / "
            f"unchanged {moves['pt_unchanged']}{pt_avg_str}."
        )
    elif moves["latest"] is not None:
        # No moves in window but we know there's history.
        parts.append(
            f"**Fundamentals — recent moves (last {moves_window_days}d):** none "
            f"(latest move {moves['latest'].isoformat()})."
        )

    # 2. Raw rating-change stream (most recent N).
    if fund.recent_rating_changes:
        parts.append("**Recent rating changes:**")
        for rc in fund.recent_rating_changes[:max_changes]:
            d = rc.grade_date.date().isoformat()
            tgt = (
                f"PT ${rc.current_price_target:.0f}"
                if rc.current_price_target is not None
                else "PT n/a"
            )
            # Surface BIG PT moves explicitly so the LLM doesn't have to do math.
            big_move = ""
            cur, prior = rc.current_price_target, rc.prior_price_target
            if cur is not None and prior is not None and prior > 0:
                pct = (cur - prior) / prior * 100
                if abs(pct) >= 5:
                    big_move = f" [PT {pct:+.0f}%]"
            line = (
                f"- {d} {rc.firm}: {rc.from_grade or '?'} → {rc.to_grade} "
                f"({rc.action or rc.price_target_action or 'n/a'}, {tgt}){big_move}"
            )
            parts.append(line)

    # 3. Rating-distribution diff (latest vs oldest period in history).
    diff_line = _format_history_diff(fund.rating_history)
    if diff_line:
        parts.append(diff_line)

    # 4. Consensus level (structural).
    cons = fund.consensus
    upside = cons.upside_pct
    upside_str = f" ({upside:+.1f}% vs current)" if upside is not None else ""
    if cons.recommendation_key or cons.target_mean:
        parts.append(
            "**Wall Street consensus (level):** "
            f"{cons.recommendation_key or '?'} "
            f"(mean={cons.recommendation_mean or '?'}, "
            f"n={cons.num_analysts or '?'} analysts); "
            f"target ${cons.target_mean or '?':.2f}{upside_str} "
            f"[low ${cons.target_low or 0:.0f} / high ${cons.target_high or 0:.0f}]"
        )
        if cons.rating_distribution:
            d = cons.rating_distribution
            parts.append(
                "**Rating distribution (latest):** "
                f"strongBuy={d.get('strongBuy', 0)}, buy={d.get('buy', 0)}, "
                f"hold={d.get('hold', 0)}, sell={d.get('sell', 0)}, "
                f"strongSell={d.get('strongSell', 0)}"
            )

    # 5. News headlines.
    if fund.news:
        parts.append("**Recent news headlines:**")
        for n in fund.news[:max_news]:
            d = n.pub_date.strftime("%Y-%m-%d") if n.pub_date else "?"
            parts.append(f"- [{d}] {n.title} ({n.publisher or '?'})")

    # 6. Earnings + blackout.
    if fund.earnings.earnings_date:
        ed = fund.earnings.earnings_date
        days_str = ""
        warning = ""
        if today is not None:
            delta = (ed - today).days
            if delta >= 0:
                days_str = f" — in {delta} day{'s' if delta != 1 else ''}"
                if delta <= blackout_days:
                    warning = (
                        f"\n**WARNING:** Earnings blackout — next earnings is in "
                        f"{delta} day(s), within the {blackout_days}-day blackout. "
                        f"Decline new BUY positions; favour HOLD / partial SELL only."
                    )
            else:
                days_str = f" — {-delta} day(s) ago (already reported)"
        parts.append(
            f"**Next earnings:** {ed.isoformat()}{days_str} "
            f"(consensus EPS ≈ ${fund.earnings.earnings_avg or 0:.2f})"
            f"{warning}"
        )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Refresh: live yfinance → fixture JSON (atomic).
# ---------------------------------------------------------------------------


def refresh_fixtures(
    codes: list[str],
    *,
    fixture_dir: str | Path | None = None,
    fetcher: Any = None,
) -> dict[str, str]:
    """Refresh the local fundamentals fixture for ``codes`` from yfinance.

    Each entry is written atomically (write to ``<name>.json.tmp`` then
    rename) so a partial mid-flight crash never leaves the runtime with a
    corrupted snapshot. Failures on one symbol don't abort the rest.

    Parameters
    ----------
    codes:
        Trading codes like ``["US.NVDA", "US.MSFT"]``. Non-US codes are
        skipped (yfinance only knows about US tickers in our setup).
    fixture_dir:
        Override the destination directory. Defaults to the package-bundled
        ``src/vibe_trader/data/fixtures/fundamentals/raw/``.
    fetcher:
        Callable ``(ticker_short: str) -> dict``. Defaults to
        :func:`vibe_trader.data.fundamentals_yfinance.fetch_raw_fundamentals`.
        Tests inject a stub to avoid touching the network.

    Returns
    -------
    Mapping ``{code: "ok" | "skipped:<reason>" | "error:<msg>"}`` so the
    cron caller can summarise the run.
    """
    if fetcher is None:
        from vibe_trader.data.fundamentals_yfinance import fetch_raw_fundamentals

        fetcher = fetch_raw_fundamentals

    root = Path(fixture_dir) if fixture_dir else _DEFAULT_FIXTURE_DIR
    root.mkdir(parents=True, exist_ok=True)

    summary: dict[str, str] = {}
    for code in codes:
        if not code.startswith("US."):
            summary[code] = "skipped:non-US"
            continue
        ticker_short = code.split(".", 1)[1]
        try:
            raw = fetcher(ticker_short)
        except Exception as exc:  # noqa: BLE001
            summary[code] = f"error:{type(exc).__name__}: {exc}"
            logger.warning("fundamentals.refresh_failed code=%s err=%s", code, exc)
            continue
        out = root / f"{code}.json"
        tmp = out.with_suffix(out.suffix + ".tmp")
        try:
            tmp.write_text(
                json.dumps(raw, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
            tmp.replace(out)
            summary[code] = "ok"
        except Exception as exc:  # noqa: BLE001
            summary[code] = f"error:write:{type(exc).__name__}: {exc}"
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
    return summary


__all__ = [
    "AnalystConsensus",
    "RatingChange",
    "NewsItem",
    "EarningsCalendar",
    "Fundamentals",
    "FundamentalsClient",
    "FixtureFundamentalsClient",
    "NullFundamentalsClient",
    "build_fundamentals_client",
    "parse_raw_fundamentals",
    "refresh_fixtures",
    "render_for_prompt",
]
