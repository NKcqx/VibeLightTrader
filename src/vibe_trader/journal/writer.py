"""Atomic writer for per-symbol journal Markdown files.

Two public entrypoints:

  - `append_event(...)`   — push a new event entry to the front and
    refresh the overview. Use for ticks that produced a SignalSuggest
    OR fired one or more Signals.
  - `refresh_overview_only(...)` — just update the overview block
    (latest price, last-check ts). Use for ticks that produced no
    signals — the user still wants the file's mtime + "最后检查时间"
    to advance so they can see the loop is alive.

Both are atomic: write a sibling tmp file, then rename. A power loss
or a Ctrl-C in the middle leaves the previous version untouched.

Concurrency: the cron loop is serial within one tick and we ship one
file per code, so there's no expected concurrency between writes.
We still write atomically to be robust against e.g. an `vibe-trader
once` invocation racing against a still-running scheduler.
"""

from __future__ import annotations

import os
import re
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

import structlog

from vibe_trader.journal.templates import (
    EVENT_DELIMITER,
    JournalEntry,
    OVERVIEW_BEGIN,
    OVERVIEW_END,
    OverviewSnapshot,
    action_emoji,
    render_event,
    render_overview,
)

# Re-exported via __init__ for convenient imports.
__all__ = [
    "JournalEntry",
    "OverviewSnapshot",
    "append_event",
    "refresh_overview_only",
    "compute_overview",
    "scan_existing_events",
]

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class _ParsedEvent:
    """Minimal parse of an existing event block.

    Used when we need to recompute the overview based on history (e.g.
    on first write of the day, or when Phase-2 hit-rate stats land).
    Today only `action` and `fallback_used` are read back; everything
    else is opaque from the writer's POV.
    """

    action: str | None
    fallback_used: bool


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def append_event(
    *,
    journal_dir: Path,
    overview: OverviewSnapshot,
    entry: JournalEntry,
) -> Path:
    """Prepend a new event entry, refresh the overview, atomically rewrite.

    Returns the resolved file path so callers can log it.
    """
    path = _path_for(journal_dir, entry.code)
    journal_dir.mkdir(parents=True, exist_ok=True)

    existing = path.read_text(encoding="utf-8") if path.exists() else ""

    # Slice off old overview/title; we always rebuild them from `overview`.
    history_md = _strip_header_and_overview(existing)

    new_event_md = render_event(entry)
    body = render_overview(overview) + "\n" + EVENT_DELIMITER + "\n\n" + new_event_md
    if history_md.strip():
        body += "\n" + EVENT_DELIMITER + "\n\n" + history_md.strip() + "\n"

    _atomic_write(path, body)
    log.info(
        "journal.append_event",
        code=entry.code,
        path=str(path),
        action=entry.suggestion.action if entry.suggestion else None,
        fallback=bool(entry.suggestion and entry.suggestion.fallback_used),
    )
    return path


def refresh_overview_only(
    *,
    journal_dir: Path,
    overview: OverviewSnapshot,
) -> Path:
    """Rewrite just the overview block; preserve every event verbatim.

    Use on no-signal ticks. If the file doesn't exist yet, we create it
    with overview + an empty history so the next event can prepend.
    """
    path = _path_for(journal_dir, overview.code)
    journal_dir.mkdir(parents=True, exist_ok=True)

    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    history_md = _strip_header_and_overview(existing)

    body = render_overview(overview)
    if history_md.strip():
        body += "\n" + EVENT_DELIMITER + "\n\n" + history_md.strip() + "\n"

    _atomic_write(path, body)
    log.debug("journal.refresh_overview", code=overview.code, path=str(path))
    return path


def scan_existing_events(path: Path) -> list[_ParsedEvent]:
    """Cheap scan: just enough to count actions / fallbacks for the overview.

    We don't try to parse full Markdown — we look for the title line of
    each event (`## <ts> — <emoji> <action>...`) and match the action
    word. Keeps the parser robust to formatting tweaks; if the title
    template changes we just lose the count, never crash.
    """
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    return _parse_event_titles(text)


def compute_overview(
    *,
    code: str,
    display_name: str | None,
    last_check_ts: datetime,
    last_price: float | None,
    intraday_pct: float | None,
    upper_threshold: float | None,
    lower_threshold: float | None,
    position_qty: int,
    avg_cost: float | None,
    unrealized_pnl: float | None,
    journal_dir: Path,
    new_entry: JournalEntry | None = None,
    hit_rate_lines: tuple[str, ...] = (),
    error_probe_lines: tuple[str, ...] = (),
) -> OverviewSnapshot:
    """Build an OverviewSnapshot by scanning history + folding in a new entry.

    `new_entry` (if given) is counted PROSPECTIVELY — i.e. the resulting
    overview includes it, even though `append_event` will be the one to
    actually persist it. This lets callers compute the overview once
    and pass it down.
    """
    path = _path_for(journal_dir, code)
    history = scan_existing_events(path)
    counts = Counter(e.action for e in history if e.action)
    fallback_count = sum(1 for e in history if e.fallback_used)

    last_decision_action: str | None = None
    last_decision_ts: datetime | None = None
    last_decision_client: str | None = None
    last_decision_confidence: float | None = None

    if new_entry is not None:
        sug = new_entry.suggestion
        if sug is not None and sug.action:
            counts[sug.action] += 1
            if sug.fallback_used:
                fallback_count += 1
        last_decision_action = sug.action if sug is not None else None
        last_decision_ts = new_entry.ts
        last_decision_client = sug.client_name if sug is not None else None
        last_decision_confidence = sug.confidence if sug is not None else None
    else:
        # No new entry — peek at the most-recent existing one for the
        # "最近决策" line. We have title-level info only (action), no
        # timestamp / confidence — render those as None so the template
        # collapses gracefully.
        most_recent = history[0] if history else None
        if most_recent is not None:
            last_decision_action = most_recent.action

    total_events = sum(counts.values())

    return OverviewSnapshot(
        code=code,
        display_name=display_name,
        last_check_ts=last_check_ts,
        last_price=last_price,
        intraday_pct=intraday_pct,
        upper_threshold=upper_threshold,
        lower_threshold=lower_threshold,
        position_qty=position_qty,
        avg_cost=avg_cost,
        unrealized_pnl=unrealized_pnl,
        total_events=total_events,
        counts_by_action=dict(counts),
        fallback_count=fallback_count,
        last_decision_action=last_decision_action,
        last_decision_ts=last_decision_ts,
        last_decision_client=last_decision_client,
        last_decision_confidence=last_decision_confidence,
        hit_rate_lines=tuple(hit_rate_lines),
        error_probe_lines=tuple(error_probe_lines),
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _path_for(journal_dir: Path, code: str) -> Path:
    """Resolve the file path for a code. Sanitises only the obvious bad chars
    so a hostile config can't write outside `journal_dir`. We don't accept
    `/` or `..` in codes — Futu codes are like `US.NVDA`, `HK.00700`, all safe.
    """
    if ".." in code:
        # Rejects both "..", "../", "...secret", and slash-replaced
        # variants like "..__etc..." after the sanitiser ran.
        raise ValueError(f"refusing journal path with '..': {code!r}")
    safe = code.replace("/", "_").replace("\\", "_")
    return journal_dir / f"{safe}.md"


def _atomic_write(path: Path, body: str) -> None:
    """Write to a sibling tmp file, fsync, rename. POSIX atomic on same fs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        # Best-effort cleanup; if rename failed the tmp is orphaned.
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


_HEADER_OR_OVERVIEW_RE = re.compile(
    r"\A(?:.*?\n)?"  # optional title line "# CODE · NAME ..."
    r"\s*" + re.escape(OVERVIEW_BEGIN) + r".*?" + re.escape(OVERVIEW_END) + r"\s*",
    re.DOTALL,
)


def _strip_header_and_overview(text: str) -> str:
    """Remove the "# header" + "<!-- overview-start --> ... <!-- overview-end -->"
    region, returning only the historical body (events + delimiters).

    If the file doesn't have an overview block (e.g. legacy / corrupt),
    return the original text untouched — never lose history.
    """
    if OVERVIEW_BEGIN not in text or OVERVIEW_END not in text:
        return text

    # Drop the title line at the very top (matched by the optional first
    # line in the regex). Then drop the overview block including its end
    # marker. After that we may have a leading blank-line + delimiter from
    # the previous body — strip them so the caller can re-prepend cleanly.
    m = _HEADER_OR_OVERVIEW_RE.match(text)
    if not m:
        return text
    rest = text[m.end():].lstrip("\n")
    if rest.startswith(EVENT_DELIMITER):
        rest = rest[len(EVENT_DELIMITER):].lstrip("\n")
    return rest


_EVENT_TITLE_RE = re.compile(
    # Title shape: "## <ts> — <emoji> <action>[<rest>]"
    # ts is anything up to the em-dash; the action word follows the
    # emoji glyph. Non-greedy so we don't span across two titles.
    r"^##\s+(?P<ts>.+?)\s+—\s+(?P<emoji>\S+)\s+(?P<action>BUY|SELL|HOLD|无决策)",
    re.MULTILINE,
)
_FALLBACK_HINT_RE = re.compile(
    r"⚠️\s*(?:走了回退路径|fallback)", re.IGNORECASE
)


def _parse_event_titles(text: str) -> list[_ParsedEvent]:
    """Title-only parse, in document order (newest first because we prepend).

    Looks at each `## ... — <emoji> ACTION` line and at the chunk between
    that line and the next one to detect a fallback marker. Robust to
    template tweaks — anything we can't recognise becomes a "no-action"
    event (action=None).
    """
    titles = list(_EVENT_TITLE_RE.finditer(text))
    out: list[_ParsedEvent] = []
    for i, m in enumerate(titles):
        action = m.group("action")
        if action == "无决策":
            action = None
        chunk_end = titles[i + 1].start() if i + 1 < len(titles) else len(text)
        chunk = text[m.end(): chunk_end]
        fallback = bool(_FALLBACK_HINT_RE.search(chunk))
        out.append(_ParsedEvent(action=action, fallback_used=fallback))
    return out


# Defensive — ensure `action_emoji` import isn't pruned by linters.
assert callable(action_emoji)


def _ensure_iterable(x: Iterable | None) -> Iterable:  # pragma: no cover
    return x if x is not None else ()
