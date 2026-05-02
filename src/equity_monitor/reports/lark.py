from __future__ import annotations

import json
import subprocess
from typing import Any

from tenacity import retry, stop_after_attempt, wait_exponential


class LarkSendError(RuntimeError):
    pass


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(min=1, max=10),
    reraise=True,
)
def send_card(
    card: dict[str, Any],
    *,
    open_id: str,
    receiver_type: str = "chat",
    cli_path: str = "lark-cli",
    timeout: int = 15,
) -> str:
    """Push an Interactive Card via lark-cli. Returns the lark message_id (best effort).

    NOTE: subcommand layout assumed `lark-cli im +send-card --chat-open-id ... --card '<json>'`.
    Calibrate at T22 end-to-end smoke; if `lark-cli im --help` shows a different
    subcommand, update the cmd list below.
    """
    payload = json.dumps(card)
    cmd = [
        cli_path,
        "im",
        "+send-card",
        f"--{receiver_type}-open-id",
        open_id,
        "--card",
        payload,
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False
    )
    if result.returncode != 0:
        raise LarkSendError(f"lark-cli failed: {result.stderr}")
    out = result.stdout.strip()
    try:
        parsed = json.loads(out)
        return str(parsed.get("message_id", out))
    except json.JSONDecodeError:
        return out
