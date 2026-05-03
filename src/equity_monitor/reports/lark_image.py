"""Send a PNG/JPG to Lark via lark-cli (Phase 3 image messages).

Mirrors the retry / error contract of `reports/lark.py:send_card`. The
underlying `lark-cli im +messages-send --image <abs-path>` command both
uploads the file and sends it as a single image message; no separate
upload/key dance is required at the caller.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Literal

from tenacity import retry, stop_after_attempt, wait_exponential

from equity_monitor.reports.lark import ReceiverType


class LarkImageError(RuntimeError):
    pass


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(min=1, max=10),
    reraise=True,
)
def send_image(
    path: Path,
    *,
    open_id: str,
    receiver_type: ReceiverType = "user",
    cli_path: str = "lark-cli",
    identity: Literal["bot", "user"] = "bot",
    timeout: int = 30,
) -> str:
    """Upload `path` and send it as an image message. Returns the lark message_id.

    Raises:
        LarkImageError: if the file is missing, lark-cli exits non-zero,
            the response is unparseable, or the API returned ok=false.
    """
    if not path.exists():
        raise LarkImageError(f"file not found: {path}")

    if receiver_type == "user":
        recipient_flag = "--user-id"
    elif receiver_type == "chat":
        recipient_flag = "--chat-id"
    else:
        raise LarkImageError(f"unknown receiver_type: {receiver_type!r}")

    cmd = [
        cli_path,
        "im",
        "+messages-send",
        "--as",
        identity,
        recipient_flag,
        open_id,
        "--image",
        str(path.absolute()),
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False
    )
    if result.returncode != 0:
        raise LarkImageError(
            f"lark-cli exit={result.returncode}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )

    out = result.stdout.strip()
    try:
        parsed = json.loads(out)
    except json.JSONDecodeError as e:
        raise LarkImageError(f"non-JSON response from lark-cli: {out[:200]}") from e

    if not parsed.get("ok", False):
        err = parsed.get("error", {})
        raise LarkImageError(
            f"lark-cli failed: {err.get('type', 'unknown')}: "
            f"{err.get('message', '')}"
        )

    msg_id = parsed.get("data", {}).get("message_id")
    if not msg_id:
        raise LarkImageError(
            f"lark-cli response missing message_id: {out[:200]}"
        )
    return str(msg_id)
