"""Send a PNG/JPG to Lark.

Thin compatibility wrapper around
:meth:`vibe_trader.lark.LarkHTTPClient.send_image`. The previous build invoked
``lark-cli im +messages-send --image`` which both uploaded and sent in one
shot; the HTTP path is two endpoints (``/im/v1/images`` then
``/im/v1/messages``) but the client hides that from callers.

Same retry contract as :mod:`vibe_trader.reports.lark`.
"""

from __future__ import annotations

from pathlib import Path

from tenacity import retry, stop_after_attempt, wait_exponential

from vibe_trader.lark.client import LarkHTTPClient
from vibe_trader.lark.errors import LarkAPIError
from vibe_trader.reports.lark import LarkSendError, ReceiverType, _to_receive_id_type


class LarkImageError(RuntimeError):
    """Raised after all retries fail uploading or sending the image."""


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
    client: LarkHTTPClient,
) -> str:
    """Upload a local image and send it as an image message.

    Returns:
        The new message's ``message_id``.

    Raises:
        LarkImageError: file missing, upload failed, send failed after all
            retries, or an unknown ``receiver_type`` was passed in.
    """
    if not path.exists():
        raise LarkImageError(f"file not found: {path}")
    try:
        receive_id_type = _to_receive_id_type(receiver_type)
    except LarkSendError as e:
        # Normalise the receiver-type guard error to the image-side type so
        # callers only need to catch one symbol.
        raise LarkImageError(str(e)) from e
    try:
        return client.send_image(
            path, receive_id=open_id, receive_id_type=receive_id_type  # type: ignore[arg-type]
        )
    except LarkAPIError as e:
        raise LarkImageError(str(e)) from e
