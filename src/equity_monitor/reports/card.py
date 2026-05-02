from __future__ import annotations

from equity_monitor.signals.base import Severity


SEVERITY_COLOR: dict[Severity, str] = {
    Severity.INFO: "grey",
    Severity.WARN: "orange",
    Severity.CRITICAL: "red",
}


SEVERITY_EMOJI: dict[Severity, str] = {
    Severity.INFO: "ℹ️",
    Severity.WARN: "⚠️",
    Severity.CRITICAL: "🔴",
}
