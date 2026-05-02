from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SentimentSnapshot:
    code: str
    ts: datetime
    temperature: float
    bullish_pct: float
    bearish_pct: float
    sample_size: int


def _parse(payload: dict) -> list[SentimentSnapshot]:
    out: list[SentimentSnapshot] = []
    for item in payload.get("snapshots", []):
        out.append(
            SentimentSnapshot(
                code=item["code"],
                ts=datetime.fromisoformat(item["ts"]),
                temperature=float(item["temperature"]),
                bullish_pct=float(item.get("bullish_pct", 0.0)),
                bearish_pct=float(item.get("bearish_pct", 0.0)),
                sample_size=int(item.get("sample_size", 0)),
            )
        )
    return out


def fetch_sentiment(
    codes: Sequence[str],
    *,
    script_path: str | Path = "~/.cursor/skills/futu-comment-sentiment/scripts/run.py",
    timeout: int = 60,
) -> list[SentimentSnapshot]:
    """NOTE: schema estimate; calibrate at T22 end-to-end smoke."""
    cmd = ["python", str(Path(script_path).expanduser()), "--codes", ",".join(codes)]
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"sentiment script failed: {result.stderr}")
    return _parse(json.loads(result.stdout))
