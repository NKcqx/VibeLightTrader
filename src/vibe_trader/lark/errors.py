from __future__ import annotations


class LarkAPIError(RuntimeError):
    """Raised on Lark OpenAPI failure.

    Two failure shapes collapse into this class:

    1. HTTP-layer failure (timeout, connection error, non-2xx) — ``code`` is
       set to the HTTP status (or ``-1`` for non-HTTP transport errors) and
       ``msg`` carries the underlying exception text.
    2. Application-layer failure (HTTP 200 but ``response.code != 0``) —
       ``code`` is the OpenAPI error code (e.g. ``99991663``) and ``msg``
       is whatever the API returned in ``msg``/``error``.

    The two are deliberately not split into subclasses: most callers either
    retry-on-anything (network) or surface the message verbatim, so a single
    flat type keeps call sites short.
    """

    def __init__(self, code: int, msg: str, *, payload: object | None = None) -> None:
        super().__init__(f"[lark] code={code}: {msg}")
        self.code = code
        self.msg = msg
        self.payload = payload
