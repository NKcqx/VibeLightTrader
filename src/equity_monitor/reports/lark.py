from __future__ import annotations

import json
import subprocess
from typing import Any, Literal

from tenacity import retry, stop_after_attempt, wait_exponential


class LarkSendError(RuntimeError):
    pass


ReceiverType = Literal["user", "chat"]


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(min=1, max=10),
    reraise=True,
)
def send_card(
    card: dict[str, Any],
    *,
    open_id: str,
    receiver_type: ReceiverType = "user",
    cli_path: str = "lark-cli",
    identity: Literal["bot", "user"] = "bot",
    timeout: int = 15,
) -> str:
    """Push an Interactive Card via lark-cli. Returns the lark message_id.

    Wraps:
        lark-cli im +messages-send --as <identity>
            --user-id ou_xxx | --chat-id oc_xxx
            --content '<card-json>' --msg-type interactive

    Args:
        card:          Lark Interactive Card dict (header/elements/...).
        open_id:       For receiver_type=user, the recipient's `ou_xxx` open_id.
                       For receiver_type=chat, the chat's `oc_xxx` chat_id.
        receiver_type: "user" → DM (uses --user-id), "chat" → group (uses --chat-id).
        identity:      "bot" (default, no extra scope) or "user"
                       (requires `lark-cli auth login --scope im:message.send_as_user`).
        timeout:       subprocess timeout in seconds.

    Raises:
        LarkSendError: if lark-cli returned non-zero exit code or response.ok is False.
    """
    if receiver_type == "user":
        recipient_flag = "--user-id"
    elif receiver_type == "chat":
        recipient_flag = "--chat-id"
    else:
        raise LarkSendError(f"unknown receiver_type: {receiver_type!r}")

    payload = json.dumps(card, ensure_ascii=False)
    cmd = [
        cli_path,
        "im",
        "+messages-send",
        "--as",
        identity,
        recipient_flag,
        open_id,
        "--content",
        payload,
        "--msg-type",
        "interactive",
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False
    )
    if result.returncode != 0:
        raise LarkSendError(
            f"lark-cli exit={result.returncode}: {result.stderr.strip() or result.stdout.strip()}"
        )

    out = result.stdout.strip()
    try:
        parsed = json.loads(out)
    except json.JSONDecodeError as e:
        raise LarkSendError(f"non-JSON response from lark-cli: {out[:200]}") from e

    if not parsed.get("ok", False):
        err = parsed.get("error", {})
        raise LarkSendError(
            f"lark-cli failed: {err.get('type', 'unknown')}: {err.get('message', '')}"
        )

    msg_id = parsed.get("data", {}).get("message_id")
    if not msg_id:
        raise LarkSendError(f"lark-cli response missing message_id: {out[:200]}")
    return str(msg_id)
