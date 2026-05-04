"""Unit tests for journal/errors.py — health probe + dev log."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from equity_monitor.journal.errors import (
    DevLogEntry,
    ProbeSummary,
    append_dev_log_entry,
    classify_exception,
    render_probe_lines,
    scan_recent_failures,
)


def _audit_row(
    *, ts: datetime, code: str = "US.NVDA",
    action: str = "HOLD",
    fallback: bool = False,
    error: dict | None = None,
) -> str:
    row: dict = {
        "ts_unix": ts.replace(tzinfo=timezone.utc).timestamp() if ts.tzinfo is None
        else ts.timestamp(),
        "code": code,
        "client": "test",
        "model": "test",
        "decision": {"action": action, "qty": 0, "reason": "x"},
        "fallback_used": fallback,
    }
    if error is not None:
        row["error"] = error
    return json.dumps(row)


# ---------------------------------------------------------------------------
# Probe scan
# ---------------------------------------------------------------------------


def test_probe_returns_healthy_for_missing_file(tmp_path):
    probe = scan_recent_failures(
        audit_log_path=tmp_path / "ghost.jsonl", code="US.NVDA"
    )
    assert isinstance(probe, ProbeSummary)
    assert probe.healthy is True
    assert probe.error_count == 0
    assert render_probe_lines(probe) == []


def test_probe_counts_errors_and_classifies(tmp_path):
    audit = tmp_path / "decisions.jsonl"
    audit.write_text(
        "\n".join(
            [
                _audit_row(ts=datetime(2026, 5, 4, 12, tzinfo=timezone.utc)),
                _audit_row(
                    ts=datetime(2026, 5, 4, 13, tzinfo=timezone.utc),
                    fallback=True,
                    error={"type": "LLMTimeoutError", "message": "slow"},
                ),
                _audit_row(
                    ts=datetime(2026, 5, 4, 14, tzinfo=timezone.utc),
                    fallback=True,
                    error={"type": "LLMParseError", "message": "no json"},
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    probe = scan_recent_failures(audit_log_path=audit, code="US.NVDA")
    assert probe.healthy is False
    assert probe.error_count == 2
    assert probe.last_failure_kind == "parse_error"
    assert "no json" in (probe.last_failure_message or "")

    rendered = render_probe_lines(probe)
    assert any("自动化健康度" in line for line in rendered)
    assert any("parse_error" in line for line in rendered)


def test_probe_ignores_other_codes(tmp_path):
    audit = tmp_path / "decisions.jsonl"
    audit.write_text(
        _audit_row(
            ts=datetime(2026, 5, 4, 13, tzinfo=timezone.utc),
            code="US.MSFT",
            fallback=True,
            error={"type": "LLMTimeoutError", "message": "slow"},
        )
        + "\n",
        encoding="utf-8",
    )
    probe = scan_recent_failures(audit_log_path=audit, code="US.NVDA")
    assert probe.healthy is True


def test_probe_lookback_caps_lines(tmp_path):
    """Only the last `lookback` lines should be considered."""
    audit = tmp_path / "decisions.jsonl"
    lines = []
    for i in range(60):
        ts = datetime(2026, 5, 1, tzinfo=timezone.utc)
        if i < 10:
            # Old errors - should be dropped by lookback=20
            lines.append(_audit_row(
                ts=ts, fallback=True,
                error={"type": "LLMTimeoutError", "message": f"old{i}"}
            ))
        else:
            lines.append(_audit_row(ts=ts))
    audit.write_text("\n".join(lines) + "\n", encoding="utf-8")

    probe = scan_recent_failures(audit_log_path=audit, code="US.NVDA", lookback=20)
    assert probe.error_count == 0  # the 10 errors were trimmed
    assert probe.lookback_count == 20


# ---------------------------------------------------------------------------
# Dev log
# ---------------------------------------------------------------------------


def test_append_dev_log_creates_file_with_header(tmp_path):
    p = tmp_path / "dev_log.md"
    entry = DevLogEntry(
        ts=datetime(2026, 5, 4, 14, 30, tzinfo=timezone.utc),
        code="US.NVDA",
        category="timeout",
        client="cursor-agent:default",
        error_type="LLMTimeoutError",
        message="cursor-agent ran for 240s without responding",
        raw_excerpt=None,
    )
    append_dev_log_entry(dev_log_path=p, entry=entry)
    text = p.read_text(encoding="utf-8")
    assert "# Equity Monitor — 开发者日志" in text
    assert "## 2026-05-04 14:30 UTC · ⏱️ timeout · US.NVDA" in text
    assert "`LLMTimeoutError`" in text


def test_append_dev_log_appends_subsequent_entries(tmp_path):
    p = tmp_path / "dev_log.md"
    entry = DevLogEntry(
        ts=datetime(2026, 5, 4, 14, 30, tzinfo=timezone.utc),
        code="US.NVDA",
        category="timeout",
        client="cursor-agent:default",
        error_type="LLMTimeoutError",
        message="msg1",
        raw_excerpt=None,
    )
    entry2 = DevLogEntry(
        ts=datetime(2026, 5, 4, 15, 30, tzinfo=timezone.utc),
        code="US.MSFT",
        category="parse_error",
        client="cursor-agent:default",
        error_type="LLMParseError",
        message="msg2",
        raw_excerpt="malformed { no closing brace",
    )
    append_dev_log_entry(dev_log_path=p, entry=entry)
    append_dev_log_entry(dev_log_path=p, entry=entry2)

    text = p.read_text(encoding="utf-8")
    # Both entries present, header only once.
    assert text.count("# Equity Monitor — 开发者日志") == 1
    assert "msg1" in text and "msg2" in text
    # Excerpt rendered as a fenced code block under details.
    assert "<details>" in text
    assert "malformed { no closing brace" in text


def test_classify_exception_known_types():
    class FakeTimeout(Exception):
        pass
    FakeTimeout.__name__ = "LLMTimeoutError"
    assert classify_exception(FakeTimeout()) == "timeout"

    class FakeParse(Exception):
        pass
    FakeParse.__name__ = "LLMParseError"
    assert classify_exception(FakeParse()) == "parse_error"

    assert classify_exception(RuntimeError("misc")) == "unknown"


def test_dev_log_truncates_huge_raw_excerpt(tmp_path):
    p = tmp_path / "dev_log.md"
    huge = "A" * 10_000  # > 4 KB cap
    entry = DevLogEntry(
        ts=datetime(2026, 5, 4, 14, 30, tzinfo=timezone.utc),
        code="US.NVDA",
        category="parse_error",
        client="x",
        error_type="LLMParseError",
        message="m",
        raw_excerpt=huge,
    )
    append_dev_log_entry(dev_log_path=p, entry=entry)
    text = p.read_text(encoding="utf-8")
    # Should NOT contain the full 10k of A's; the body's A-run is bounded by 4096.
    assert "A" * 4096 in text
    assert "A" * 4097 not in text
