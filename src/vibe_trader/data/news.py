from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True, slots=True)
class NewsItem:
    code: str
    ts: datetime
    source: str | None
    title: str
    url: str
    summary: str | None


def _parse(payload: dict) -> list[NewsItem]:
    out: list[NewsItem] = []
    for code, items in payload.get("by_code", {}).items():
        for it in items:
            out.append(
                NewsItem(
                    code=code,
                    ts=datetime.fromisoformat(it["ts"]),
                    source=it.get("source"),
                    title=it["title"],
                    url=it["url"],
                    summary=it.get("summary"),
                )
            )
    return out


def fetch_news_digest(
    codes: Sequence[str],
    *,
    script_path: str | Path = "~/.cursor/skills/futu-stock-digest/scripts/run.py",
    timeout: int = 60,
) -> list[NewsItem]:
    """NOTE: schema estimate; calibrate at T22 end-to-end smoke."""
    cmd = ["python", str(Path(script_path).expanduser()), "--codes", ",".join(codes)]
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"news script failed: {result.stderr}")
    return _parse(json.loads(result.stdout))
