"""Tenant-access-token cache for the Lark/Feishu OpenAPI.

A single :class:`TokenManager` instance is shared by all requests issued
through one :class:`LarkHTTPClient` — the manager calls
``/open-apis/auth/v3/tenant_access_token/internal`` lazily and only on
miss / near-expiry. Refresh is done eagerly under a lock so concurrent
callers all see one fresh token (and one network round-trip).

The token's TTL is whatever the API returned, minus a 30s safety margin
to avoid mid-flight expiry on the wire.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vibe_trader.lark.errors import LarkAPIError

if TYPE_CHECKING:
    import httpx


_TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"
_REFRESH_MARGIN_SECONDS = 30.0


@dataclass(slots=True)
class _CachedToken:
    value: str
    expires_at: float

    def is_valid(self, now: float) -> bool:
        return now + _REFRESH_MARGIN_SECONDS < self.expires_at


class TokenManager:
    """Lazy + cached tenant_access_token fetcher.

    Construct once per app process. Pass either an ``httpx.Client`` (the
    client owns the lifecycle, recommended for prod) or rely on the
    auto-created internal client (fine for tests / one-shot scripts).

    The class is thread-safe: concurrent ``get()`` calls during refresh
    serialize on a single mutex so only one network round-trip happens.
    """

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        *,
        base_url: str = "https://open.feishu.cn",
        http_client: "httpx.Client | None" = None,
        timeout_s: float = 10.0,
    ) -> None:
        if not app_id:
            raise ValueError("TokenManager: app_id is required")
        if not app_secret:
            raise ValueError("TokenManager: app_secret is required")
        self._app_id = app_id
        self._app_secret = app_secret
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._lock = threading.Lock()
        self._cached: _CachedToken | None = None
        self._http: httpx.Client | None = http_client
        self._owns_http = http_client is None

    def _ensure_http(self) -> "httpx.Client":
        if self._http is None:
            import httpx

            self._http = httpx.Client(timeout=self._timeout_s)
        return self._http

    def get(self, *, force_refresh: bool = False) -> str:
        """Return a valid tenant_access_token, refreshing if needed."""
        now = time.time()
        with self._lock:
            if (
                not force_refresh
                and self._cached is not None
                and self._cached.is_valid(now)
            ):
                return self._cached.value
            self._cached = self._fetch()
            return self._cached.value

    def invalidate(self) -> None:
        """Drop the cached token; next ``get()`` will refresh."""
        with self._lock:
            self._cached = None

    def close(self) -> None:
        """Close the internal http client if we own it. Idempotent."""
        if self._owns_http and self._http is not None:
            try:
                self._http.close()
            except Exception:
                pass
            self._http = None

    # ------------------------------------------------------------------

    def _fetch(self) -> _CachedToken:
        url = self._base_url + _TOKEN_PATH
        body = {"app_id": self._app_id, "app_secret": self._app_secret}
        http = self._ensure_http()
        try:
            resp = http.post(url, json=body, timeout=self._timeout_s)
        except Exception as e:  # connection / timeout / DNS
            raise LarkAPIError(-1, f"token fetch transport error: {e}") from e

        if resp.status_code != 200:
            raise LarkAPIError(
                resp.status_code, f"token fetch HTTP {resp.status_code}: {resp.text[:300]}"
            )
        try:
            payload = resp.json()
        except ValueError as e:
            raise LarkAPIError(-1, f"token fetch non-JSON: {resp.text[:300]}") from e

        code = int(payload.get("code", -1))
        if code != 0:
            raise LarkAPIError(
                code,
                str(payload.get("msg", "unknown")),
                payload=payload,
            )

        token = payload.get("tenant_access_token")
        expire = payload.get("expire")
        if not token or not isinstance(expire, int):
            raise LarkAPIError(
                -1,
                f"token fetch malformed payload: missing tenant_access_token/expire ({payload})",
                payload=payload,
            )

        return _CachedToken(value=str(token), expires_at=time.time() + float(expire))
