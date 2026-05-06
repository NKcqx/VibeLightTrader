"""Per-symbol Markdown journal — human-readable trace of every event.

Why this module exists:

The cron loop is deliberately quiet on Lark (one card per actionable
event, nothing else). That keeps the user's IM clean but it makes the
*process* opaque — between two cards the user can't see what was
checked, what indicators looked like, what the LLM thought, or why a
signal was or wasn't acted on. The journal fills that gap without
adding chat noise: each watchlist symbol gets one Markdown file under
`data/journal/<code>.md` that's append-only for events and live-rewritten
for the overview block.

File layout::

    data/journal/
        US.NVDA.md
        US.MSFT.md

Each file is two parts separated by HTML markers so we can rewrite the
overview without disturbing the history::

    # <code> · <name> 监控日志

    <!-- overview-start -->
    ## 当前状态
    ... refreshed every tick ...
    <!-- overview-end -->

    ---

    ## <ts> — <action emoji> <label>
    ... event entry ...

    ---
    ... older entries ...

The writer is small on purpose: just two public functions
(`append_event`, `refresh_overview_only`). Each one rewrites the file
atomically (tmp → rename) so a partial write or crash leaves the
previous version intact. We don't lock the file — the cron loop runs
serially per (code, tick) and the writer is the only producer.

Invocations live in `scheduler/jobs.py:run_intraday_check` step 7+.
The CLI never writes here directly today but `vibe-trader decide
submit` could, in a follow-up, drop a "human override" event entry.
"""

from vibe_trader.journal.writer import (
    JournalEntry,
    OverviewSnapshot,
    append_event,
    refresh_overview_only,
)

__all__ = [
    "JournalEntry",
    "OverviewSnapshot",
    "append_event",
    "refresh_overview_only",
]
