"""Send Lark Interactive Cards.

Thin compatibility wrapper around :class:`vibe_trader.lark.LarkHTTPClient`. The
former implementation shelled out to a Node ``lark-cli`` binary which is not
publicly distributed; this module now talks to the public Lark/Feishu OpenAPI
directly via HTTP. The function signature is preserved as much as practical
so most call sites only need to swap ``cli_path=...`` for ``client=...``.

Retry policy is unchanged (3 attempts, exp-backoff), so transient 5xx /
connection errors heal on their own.
"""

from __future__ import annotations

from typing import Any, Literal

from tenacity import retry, stop_after_attempt, wait_exponential

from vibe_trader.lark.client import LarkHTTPClient
from vibe_trader.lark.errors import LarkAPIError


class LarkSendError(RuntimeError):
    """Raised after all retries fail. Carries the underlying API message."""


ReceiverType = Literal["user", "chat"]


def _to_receive_id_type(receiver_type: ReceiverType) -> str:
    if receiver_type == "user":
        return "open_id"
    if receiver_type == "chat":
        return "chat_id"
    raise LarkSendError(f"unknown receiver_type: {receiver_type!r}")


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
    client: LarkHTTPClient,
) -> str:
    """Push an Interactive Card to a user (open_id) or chat (chat_id).

    Args:
        card:          Lark Interactive Card dict (header / elements / ...).
        open_id:       For ``receiver_type='user'`` an ``ou_xxx`` open_id;
                       for ``receiver_type='chat'`` an ``oc_xxx`` chat_id.
                       The argument name is preserved for backwards compat.
        receiver_type: ``'user'`` → DM, ``'chat'`` → group.
        client:        A live :class:`LarkHTTPClient`. Construct one per
                       process; it amortises the auth round-trip.

    Returns:
        The new message's ``message_id``.

    Raises:
        LarkSendError: when all retries are exhausted or the API returned a
            non-zero ``code`` that isn't transient.
    """
    receive_id_type = _to_receive_id_type(receiver_type)
    try:
        return client.send_card(
            card, receive_id=open_id, receive_id_type=receive_id_type  # type: ignore[arg-type]
        )
    except LarkAPIError as e:
        raise LarkSendError(str(e)) from e
