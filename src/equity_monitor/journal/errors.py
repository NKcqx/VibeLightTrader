"""Error probe + developer log for the journal layer.

Two artifacts:

  1. **Error probe** — a ProbeSummary computed from the tail of
     `data/llm_decisions.jsonl`. Surfaces fallback/error counts and the
     most-recent failure for the per-symbol overview block, so the
     human can see "anything wrong with this symbol's automation lately"
     without reading any logs.

  2. **Developer log** (`data/dev_log.md`) — a single shared Markdown
     file where every LLM-side issue is recorded as one entry. Unlike
     the per-symbol journal which is for the *human reviewer* of trades,
     this file is for *whoever debugs the pipeline*. Entries cover:
       - cursor-agent timeouts
       - LLM parse errors (response wasn't valid Decision JSON)
       - Constraint violations (LLM proposed an out-of-bounds trade)
       - Rate-limit / HTTP / auth failures
       - Any unexpected fallback to the rule path

The dev log is purposefully simple: no rotation, no JSON sidecar. We
trust git not to hold it (data/ is in .gitignore) and we cap individual
entries' raw_text to 4 KB so a runaway model output can't blow the file
to gigabytes.

LLMStrategy is the only producer right now. If a future strategy fails
in a way that should land in the dev log, just import and call
`append_dev_log_entry`.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ProbeSummary:
    """Result of a quick "anything wrong recently?" scan over the audit log.

    `lookback_count` is how many trailing lines we considered (capped to
    keep the scan O(N) where N is the cap). The window is "the last N
    audit-log lines for this code", not a time window — fits the user's
    question better than e.g. "last 24h" because rare codes might have
    no events in 24h.
    """

    code: str
    lookback_count: int
    fallback_count: int
    error_count: int
    last_failure_ts: datetime | None
    last_failure_kind: str | None  # "fallback" / "parse" / "timeout" / "http" / "constraint"
    last_failure_message: str | None

    @property
    def healthy(self) -> bool:
        return self.fallback_count == 0 and self.error_count == 0


def scan_recent_failures(
    *, audit_log_path: Path, code: str, lookback: int = 50
) -> ProbeSummary:
    """Scan the last `lookback` audit-log lines for `code`; summarise failures.

    Cheap by design (single sequential read of the tail). Doesn't crash
    on missing file or malformed lines; an empty / corrupt log just
    yields a healthy probe.
    """
    if not audit_log_path.exists():
        return ProbeSummary(code, 0, 0, 0, None, None, None)

    lines = _tail_lines(audit_log_path, lookback)
    fallback_count = 0
    error_count = 0
    last_ts: datetime | None = None
    last_kind: str | None = None
    last_msg: str | None = None

    for ln in lines:
        try:
            row = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if row.get("code") != code:
            continue
        ts_unix = row.get("ts_unix")
        ts = (
            datetime.fromtimestamp(ts_unix, tz=timezone.utc)
            if isinstance(ts_unix, (int, float))
            else None
        )
        is_fallback = bool(row.get("fallback_used"))
        err = row.get("error")

        if err:
            error_count += 1
            last_ts = ts
            last_msg = str(err.get("message", ""))[:300]
            last_kind = _classify_error(err.get("type"), is_fallback)
        elif is_fallback:
            # Fallback without an error is rare (constraint violation
            # path with the explicit raise / re-raise sequence). Still
            # worth flagging.
            fallback_count += 1
            last_ts = last_ts or ts
            last_kind = last_kind or "fallback"

    return ProbeSummary(
        code=code,
        lookback_count=len(lines),
        fallback_count=fallback_count,
        error_count=error_count,
        last_failure_ts=last_ts,
        last_failure_kind=last_kind,
        last_failure_message=last_msg,
    )


def render_probe_lines(probe: ProbeSummary) -> list[str]:
    """Render zero or more bullet lines for the overview block.

    Healthy code → empty list (overview omits the section). Unhealthy
    → one summary line + an optional last-failure detail line.
    """
    if probe.healthy:
        return []
    parts: list[str] = []
    if probe.error_count or probe.fallback_count:
        parts.append(
            f"- **⚠️ 自动化健康度**："
            f"近 {probe.lookback_count} 次决策中，"
            f"{probe.error_count} 次报错，{probe.fallback_count} 次 fallback"
        )
    if probe.last_failure_ts is not None:
        ts_str = probe.last_failure_ts.strftime("%Y-%m-%d %H:%M UTC")
        kind = probe.last_failure_kind or "fallback"
        msg = (probe.last_failure_message or "")[:140]
        parts.append(f"  - 最近一次：{ts_str} · {kind}" + (f" — {msg}" if msg else ""))
    return parts


# ---------------------------------------------------------------------------
# Developer log: append-only Markdown
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DevLogEntry:
    """One developer-log incident entry.

    `category` is a short tag — used both as the icon mapping and for
    grep-friendly fixed-form filtering.
    """

    ts: datetime
    code: str
    category: str  # "timeout" / "parse_error" / "http" / "auth" / "rate_limit" / "constraint" / "fallback" / "unknown"
    client: str  # e.g. "cursor-agent:default", "anthropic:claude-3-5-sonnet"
    error_type: str  # exception class name
    message: str  # short reason, ≤ 300 chars
    raw_excerpt: str | None  # optional excerpt of LLM output (≤ 4 KB)


def append_dev_log_entry(
    *,
    dev_log_path: Path,
    entry: DevLogEntry,
) -> None:
    """Append one entry to the dev log. Best-effort; never raises."""
    try:
        dev_log_path.parent.mkdir(parents=True, exist_ok=True)
        if not dev_log_path.exists():
            _atomic_write_initial(dev_log_path)

        body = _render_entry_md(entry) + "\n"
        with dev_log_path.open("a", encoding="utf-8") as f:
            f.write(body)
    except Exception as e:  # pragma: no cover — io is non-critical
        log.warning(
            "dev_log.append_failed",
            path=str(dev_log_path),
            error=repr(e),
        )


def trim_dev_log_to_recent(
    *, dev_log_path: Path, max_age_days: int = 30
) -> None:  # pragma: no cover — opt-in maintenance, not in cron yet
    """Optional: drop entries older than `max_age_days`. Not currently called
    from the cron loop — invoke manually when the file gets uncomfortably
    long. Keeps the file's leading title.
    """
    if not dev_log_path.exists():
        return
    text = dev_log_path.read_text(encoding="utf-8")
    head, _sep, body = text.partition("\n\n")
    if "\n## " not in body:
        return
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=max_age_days)
    kept_blocks: list[str] = []
    for block in body.split("\n## "):
        if not block.strip():
            continue
        ts_line = block.split("\n", 1)[0]
        # First line is e.g. "2026-05-04 06:30 UTC · timeout · ..."
        try:
            ts_str = ts_line.split(" UTC")[0]
            ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            kept_blocks.append(block)
            continue
        if ts >= cutoff:
            kept_blocks.append(block)
    new_body = "\n## ".join(kept_blocks) if kept_blocks else ""
    new_text = head + "\n\n" + ("## " + new_body if new_body else "")
    dev_log_path.write_text(new_text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


_CATEGORY_ICON = {
    "timeout": "⏱️",
    "parse_error": "🧩",
    "http": "🌐",
    "auth": "🔒",
    "rate_limit": "🚦",
    "constraint": "🚫",
    "fallback": "↩️",
    "unknown": "❓",
}


def _classify_error(exc_type: str | None, is_fallback: bool) -> str:
    if not exc_type:
        return "fallback" if is_fallback else "unknown"
    table = {
        "LLMTimeoutError": "timeout",
        "LLMParseError": "parse_error",
        "LLMAuthError": "auth",
        "LLMRateLimitError": "rate_limit",
        "LLMHTTPError": "http",
        "LLMError": "http",
        "ConstraintViolation": "constraint",
    }
    return table.get(exc_type, "unknown")


def classify_exception(exc: Exception) -> str:
    """Public version of `_classify_error` for callers that have an Exception
    in hand (LLMStrategy fallback path uses this to tag the dev log)."""
    return _classify_error(type(exc).__name__, is_fallback=False)


def _render_entry_md(entry: DevLogEntry) -> str:
    icon = _CATEGORY_ICON.get(entry.category, "❓")
    lines = [
        f"## {entry.ts.strftime('%Y-%m-%d %H:%M UTC')} · {icon} {entry.category} · {entry.code}",
        "",
        f"- **client**: `{entry.client}`",
        f"- **error**: `{entry.error_type}` — {entry.message}",
    ]
    if entry.raw_excerpt:
        excerpt = entry.raw_excerpt[:4096]
        lines.append("")
        lines.append("<details><summary>原始模型输出（节选）</summary>")
        lines.append("")
        lines.append("```")
        lines.append(excerpt)
        lines.append("```")
        lines.append("")
        lines.append("</details>")
    return "\n".join(lines) + "\n"


def _atomic_write_initial(path: Path) -> None:
    """Write the dev log file with its title atomically."""
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    header = (
        "# Equity Monitor — 开发者日志\n\n"
        "记录 LLM 调用 / 解析 / 约束失败的事件。每次 cron tick 出错时会追加一条。\n"
        "**不要把这个文件给非技术用户看** — 它是给排障人员的。\n"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(header)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _tail_lines(path: Path, n: int) -> list[str]:
    """Return the last `n` non-empty lines from `path`.

    Naive: reads the whole file. The audit log is small (< few MB) by
    design and read once per overview rebuild — not a hot path. If we
    ever push past 50 MB we can swap in a reverse-seek implementation.
    """
    text = path.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return lines[-n:]
