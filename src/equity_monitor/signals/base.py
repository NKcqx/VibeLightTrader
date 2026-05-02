from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class Severity(str, Enum):
    INFO = "INFO"
    WARN = "WARN"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True, slots=True)
class Signal:
    code: str
    ts: datetime
    signal_type: str
    severity: Severity
    payload: dict[str, Any] = field(default_factory=dict)
