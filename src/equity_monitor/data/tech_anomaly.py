from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True, slots=True)
class TechAnomaly:
    code: str
    ts: datetime
    indicator: str
    event: str
    description: str


def _parse(payload: dict) -> list[TechAnomaly]:
    out: list[TechAnomaly] = []
    for item in payload.get("anomalies", []):
        out.append(
            TechAnomaly(
                code=item["code"],
                ts=datetime.fromisoformat(item["ts"]),
                indicator=item["indicator"],
                event=item["event"],
                description=item.get("description", ""),
            )
        )
    return out


def fetch_tech_anomalies(
    codes: Sequence[str],
    *,
    script_path: str | Path = "~/.cursor/skills/futu-technical-anomaly/scripts/run.py",
    timeout: int = 30,
) -> list[TechAnomaly]:
    """Invoke Futu Technical Anomaly script via subprocess; parse stdout JSON.

    NOTE: schema (indicator/event/description) is best-effort estimate from spec;
    expect calibration during T22 end-to-end smoke once OpenD + the skill
    actually run live.
    """
    cmd = ["python", str(Path(script_path).expanduser()), "--codes", ",".join(codes)]
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"tech_anomaly script failed: {result.stderr}")
    return _parse(json.loads(result.stdout))
