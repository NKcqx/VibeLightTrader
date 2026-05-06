from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True, slots=True)
class CapitalAnomaly:
    code: str
    ts: datetime
    flow_type: str
    amount: float
    description: str


def _parse(payload: dict) -> list[CapitalAnomaly]:
    out: list[CapitalAnomaly] = []
    for item in payload.get("anomalies", []):
        out.append(
            CapitalAnomaly(
                code=item["code"],
                ts=datetime.fromisoformat(item["ts"]),
                flow_type=item["flow_type"],
                amount=float(item.get("amount", 0.0)),
                description=item.get("description", ""),
            )
        )
    return out


def fetch_capital_anomalies(
    codes: Sequence[str],
    *,
    script_path: str | Path = "~/.cursor/skills/futu-capital-anomaly/scripts/run.py",
    timeout: int = 30,
) -> list[CapitalAnomaly]:
    """NOTE: schema estimate; calibrate at T22 end-to-end smoke."""
    cmd = ["python", str(Path(script_path).expanduser()), "--codes", ",".join(codes)]
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"capital_anomaly script failed: {result.stderr}")
    return _parse(json.loads(result.stdout))
