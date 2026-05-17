"""Lark/Feishu OpenAPI HTTP transport.

Replaces the previous `lark-cli` subprocess shim with direct calls to
``open.feishu.cn`` / ``open.larksuite.com``. The package is self-contained:
no Node binary required, no internal-registry tarball.

Auth model
----------
Standard "Custom App" two-leg flow:

  app_id + app_secret  --POST /open-apis/auth/v3/tenant_access_token/internal-->  tenant_access_token
                                                                                       │
  every subsequent /open-apis/* call attaches Authorization: Bearer <token> ◄──────────┘

The token is cached in-process for ``token.expire - 30s`` and refreshed on
demand by :class:`vibe_trader.lark.auth.TokenManager`.

Public surface
--------------
- :class:`vibe_trader.lark.client.LarkHTTPClient`  — send_card / send_text /
  send_image / upload_image / list_messages / resolve_p2p_chat_id.
- :class:`vibe_trader.lark.auth.TokenManager`     — token cache (testable in
  isolation; injected into the client).
- :class:`vibe_trader.lark.errors.LarkAPIError`   — non-zero ``code`` from
  the API, or transport-level failure.
"""

from vibe_trader.lark.auth import TokenManager
from vibe_trader.lark.client import LarkHTTPClient
from vibe_trader.lark.errors import LarkAPIError

__all__ = ["LarkAPIError", "LarkHTTPClient", "TokenManager"]
