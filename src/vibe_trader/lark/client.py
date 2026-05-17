"""HTTP-only Lark/Feishu OpenAPI client.

Replaces every former ``lark-cli`` subprocess call. Surface mirrors what the
old shim exposed so the transport swap is mostly mechanical for callers:

  - :py:meth:`LarkHTTPClient.send_card`  — Interactive Card via /im/v1/messages
  - :py:meth:`LarkHTTPClient.send_text`  — Markdown text via /im/v1/messages
  - :py:meth:`LarkHTTPClient.send_image` — image_key resolved + /im/v1/messages
  - :py:meth:`LarkHTTPClient.upload_image`         — /im/v1/images (multipart)
  - :py:meth:`LarkHTTPClient.list_chat_messages`   — /im/v1/messages (GET)
  - :py:meth:`LarkHTTPClient.resolve_p2p_chat_id`  — sends a benign init
    message, returns the chat_id (Lark p2p chats are implicit).

All endpoints follow the standard envelope ``{"code", "msg", "data"}``;
any non-zero ``code`` raises :class:`vibe_trader.lark.errors.LarkAPIError`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from vibe_trader.lark.auth import TokenManager
from vibe_trader.lark.errors import LarkAPIError

if TYPE_CHECKING:
    import httpx


ReceiveIdType = Literal["open_id", "chat_id", "user_id", "union_id", "email"]
ContainerIdType = Literal["chat", "thread"]


class LarkHTTPClient:
    """Thin wrapper around the Lark OpenAPI REST endpoints we actually use.

    Reusing one instance across many calls is the recommended pattern: the
    underlying ``httpx.Client`` keeps connections warm, and the
    :class:`TokenManager` amortises the auth round-trip.
    """

    def __init__(
        self,
        token_manager: TokenManager,
        *,
        base_url: str = "https://open.feishu.cn",
        http_client: "httpx.Client | None" = None,
        timeout_s: float = 15.0,
    ) -> None:
        self._tm = token_manager
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._http: httpx.Client | None = http_client
        self._owns_http = http_client is None

    # ------------------------------------------------------------------
    # connection lifecycle
    # ------------------------------------------------------------------

    def _ensure_http(self) -> "httpx.Client":
        if self._http is None:
            import httpx

            self._http = httpx.Client(timeout=self._timeout_s)
        return self._http

    def close(self) -> None:
        """Close internal http client if we own it. Idempotent."""
        if self._owns_http and self._http is not None:
            try:
                self._http.close()
            except Exception:
                pass
            self._http = None

    def __enter__(self) -> LarkHTTPClient:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ------------------------------------------------------------------
    # private helpers
    # ------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._tm.get()}",
            "Content-Type": "application/json; charset=utf-8",
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        files: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        """Issue a request and parse the standard envelope.

        On a 401 ("Invalid access token") we invalidate the cached token and
        retry exactly once — handles the rare case of clock skew between
        Lark and us, or a token revoked out-of-band.
        """
        url = self._base_url + path
        http = self._ensure_http()
        timeout = timeout_s if timeout_s is not None else self._timeout_s

        for attempt in (1, 2):
            headers = self._headers()
            if files is not None:
                # let httpx set the multipart content-type boundary
                headers.pop("Content-Type", None)
            try:
                if files is not None:
                    resp = http.request(
                        method,
                        url,
                        headers=headers,
                        params=params,
                        data=data,
                        files=files,
                        timeout=timeout,
                    )
                else:
                    resp = http.request(
                        method,
                        url,
                        headers=headers,
                        params=params,
                        json=json_body,
                        timeout=timeout,
                    )
            except Exception as e:
                raise LarkAPIError(-1, f"{method} {path} transport error: {e}") from e

            if resp.status_code == 401 and attempt == 1:
                self._tm.invalidate()
                continue
            break

        if resp.status_code != 200:
            raise LarkAPIError(
                resp.status_code,
                f"{method} {path} HTTP {resp.status_code}: {resp.text[:300]}",
            )

        try:
            payload = resp.json()
        except ValueError as e:
            raise LarkAPIError(
                -1, f"{method} {path} non-JSON response: {resp.text[:300]}"
            ) from e

        code = int(payload.get("code", -1))
        if code != 0:
            raise LarkAPIError(
                code,
                str(payload.get("msg", "unknown")),
                payload=payload,
            )
        return payload

    # ------------------------------------------------------------------
    # message sending
    # ------------------------------------------------------------------

    def _send_message(
        self,
        *,
        receive_id: str,
        receive_id_type: ReceiveIdType,
        msg_type: str,
        content: str,
    ) -> str:
        """Low-level wrapper around POST /open-apis/im/v1/messages.

        ``content`` MUST already be a JSON-encoded string per the API spec
        (``{"text": "..."}`` for text, the card body for ``interactive``,
        ``{"image_key": "..."}`` for images).

        Returns the new message_id.
        """
        payload = self._request(
            "POST",
            "/open-apis/im/v1/messages",
            params={"receive_id_type": receive_id_type},
            json_body={
                "receive_id": receive_id,
                "msg_type": msg_type,
                "content": content,
            },
        )
        msg_id = payload.get("data", {}).get("message_id")
        if not msg_id:
            raise LarkAPIError(
                -1, f"send_message response missing message_id: {payload}"
            )
        return str(msg_id)

    def send_card(
        self,
        card: dict[str, Any],
        *,
        receive_id: str,
        receive_id_type: ReceiveIdType = "open_id",
    ) -> str:
        """Send a Lark Interactive Card to a user (open_id) or chat (chat_id)."""
        return self._send_message(
            receive_id=receive_id,
            receive_id_type=receive_id_type,
            msg_type="interactive",
            content=json.dumps(card, ensure_ascii=False),
        )

    def send_text(
        self,
        text: str,
        *,
        receive_id: str,
        receive_id_type: ReceiveIdType = "open_id",
    ) -> str:
        """Send a plain text message (Lark's Markdown subset is rendered client-side)."""
        return self._send_message(
            receive_id=receive_id,
            receive_id_type=receive_id_type,
            msg_type="text",
            content=json.dumps({"text": text}, ensure_ascii=False),
        )

    def send_image(
        self,
        path: Path,
        *,
        receive_id: str,
        receive_id_type: ReceiveIdType = "open_id",
    ) -> str:
        """Upload ``path`` then send it as an image message. Returns message_id."""
        if not path.exists():
            raise LarkAPIError(-1, f"file not found: {path}")
        image_key = self.upload_image(path)
        return self._send_message(
            receive_id=receive_id,
            receive_id_type=receive_id_type,
            msg_type="image",
            content=json.dumps({"image_key": image_key}, ensure_ascii=False),
        )

    # ------------------------------------------------------------------
    # image upload
    # ------------------------------------------------------------------

    def upload_image(self, path: Path, *, image_type: str = "message") -> str:
        """Upload a local PNG/JPG. Returns the resulting ``image_key``."""
        if not path.exists():
            raise LarkAPIError(-1, f"file not found: {path}")
        with open(path, "rb") as fp:
            files = {"image": (path.name, fp, "application/octet-stream")}
            data = {"image_type": image_type}
            payload = self._request(
                "POST",
                "/open-apis/im/v1/images",
                files=files,
                data=data,
                timeout_s=max(self._timeout_s, 30.0),
            )
        image_key = payload.get("data", {}).get("image_key")
        if not image_key:
            raise LarkAPIError(
                -1, f"upload_image response missing image_key: {payload}"
            )
        return str(image_key)

    # ------------------------------------------------------------------
    # message reading (for the polling listener)
    # ------------------------------------------------------------------

    def list_chat_messages(
        self,
        *,
        chat_id: str,
        start_time: int | None = None,
        end_time: int | None = None,
        page_size: int = 20,
        sort_type: Literal["ByCreateTimeAsc", "ByCreateTimeDesc"] = "ByCreateTimeDesc",
        page_token: str | None = None,
        container_id_type: ContainerIdType = "chat",
    ) -> dict[str, Any]:
        """GET /open-apis/im/v1/messages — list a chat's recent messages.

        ``start_time`` / ``end_time`` are unix-seconds (string-coerced for
        the API). Returns the raw ``data`` dict so callers can read both
        ``items`` and ``page_token`` for paging.
        """
        params: dict[str, Any] = {
            "container_id_type": container_id_type,
            "container_id": chat_id,
            "page_size": page_size,
            "sort_type": sort_type,
        }
        if start_time is not None:
            params["start_time"] = str(int(start_time))
        if end_time is not None:
            params["end_time"] = str(int(end_time))
        if page_token:
            params["page_token"] = page_token
        payload = self._request("GET", "/open-apis/im/v1/messages", params=params)
        return payload.get("data") or {}

    # ------------------------------------------------------------------
    # p2p chat-id resolution
    # ------------------------------------------------------------------

    def resolve_p2p_chat_id(
        self, recipient_open_id: str, *, init_text: str = "🟢 listener online"
    ) -> str:
        """Send a benign init ping to the user; return the resulting chat_id.

        Lark p2p chats are implicit — there's no explicit "create p2p" API,
        but every successful ``send_message`` returns the ``chat_id`` once
        the conversation exists. This helper hides the dance from callers.
        """
        payload = self._request(
            "POST",
            "/open-apis/im/v1/messages",
            params={"receive_id_type": "open_id"},
            json_body={
                "receive_id": recipient_open_id,
                "msg_type": "text",
                "content": json.dumps({"text": init_text}, ensure_ascii=False),
            },
        )
        chat_id = payload.get("data", {}).get("chat_id")
        if not chat_id:
            raise LarkAPIError(
                -1, f"resolve_p2p_chat_id missing chat_id in response: {payload}"
            )
        return str(chat_id)


# ----------------------------------------------------------------------
# convenience constructor
# ----------------------------------------------------------------------


def build_client_from_env(
    *,
    app_id: str,
    app_secret: str,
    base_url: str = "https://open.feishu.cn",
    timeout_s: float = 15.0,
) -> LarkHTTPClient:
    """Build a :class:`LarkHTTPClient` from raw credentials.

    Convenience for one-shot scripts; production code threads
    :class:`TokenManager` itself so it can be shared with other clients.
    """
    tm = TokenManager(app_id=app_id, app_secret=app_secret, base_url=base_url)
    return LarkHTTPClient(tm, base_url=base_url, timeout_s=timeout_s)
