from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from vibe_trader.lark.auth import TokenManager
from vibe_trader.lark.errors import LarkAPIError


def _ok_resp(token: str = "t_abc", expire: int = 7200) -> MagicMock:
    m = MagicMock()
    m.status_code = 200
    m.json.return_value = {
        "code": 0,
        "msg": "ok",
        "tenant_access_token": token,
        "expire": expire,
    }
    return m


def _api_err_resp(code: int = 99991663, msg: str = "Invalid app_secret") -> MagicMock:
    m = MagicMock()
    m.status_code = 200
    m.json.return_value = {"code": code, "msg": msg}
    return m


def _http_err_resp(status: int = 500, body: str = "boom") -> MagicMock:
    m = MagicMock()
    m.status_code = status
    m.text = body
    m.json.side_effect = ValueError("not json")
    return m


def test_constructor_requires_credentials() -> None:
    with pytest.raises(ValueError, match="app_id"):
        TokenManager(app_id="", app_secret="x")
    with pytest.raises(ValueError, match="app_secret"):
        TokenManager(app_id="x", app_secret="")


def test_get_fetches_and_caches_token() -> None:
    http = MagicMock()
    http.post.return_value = _ok_resp("token_aaa", expire=7200)
    tm = TokenManager(
        app_id="cli_x", app_secret="sec", http_client=http, base_url="https://open.feishu.cn"
    )

    assert tm.get() == "token_aaa"
    assert tm.get() == "token_aaa"
    assert http.post.call_count == 1, "second get() should hit the cache"


def test_get_force_refresh_bypasses_cache() -> None:
    http = MagicMock()
    http.post.side_effect = [
        _ok_resp("first", expire=7200),
        _ok_resp("second", expire=7200),
    ]
    tm = TokenManager(app_id="cli_x", app_secret="sec", http_client=http)

    assert tm.get() == "first"
    assert tm.get(force_refresh=True) == "second"
    assert http.post.call_count == 2


def test_invalidate_forces_next_get_to_refetch() -> None:
    http = MagicMock()
    http.post.side_effect = [
        _ok_resp("first", expire=7200),
        _ok_resp("second", expire=7200),
    ]
    tm = TokenManager(app_id="cli_x", app_secret="sec", http_client=http)

    tm.get()
    tm.invalidate()
    assert tm.get() == "second"


def test_get_refreshes_when_token_near_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    """expire=10s, REFRESH_MARGIN_SECONDS=30 → cached token never satisfies is_valid()."""
    http = MagicMock()
    http.post.side_effect = [
        _ok_resp("first", expire=10),
        _ok_resp("second", expire=10),
    ]
    tm = TokenManager(app_id="cli_x", app_secret="sec", http_client=http)

    assert tm.get() == "first"
    assert tm.get() == "second", "near-expiry token should not be reused"
    assert http.post.call_count == 2


def test_get_called_url_and_body() -> None:
    http = MagicMock()
    http.post.return_value = _ok_resp("t", expire=7200)
    tm = TokenManager(
        app_id="cli_x",
        app_secret="sec",
        http_client=http,
        base_url="https://open.feishu.cn",
    )
    tm.get()
    args, kwargs = http.post.call_args
    assert args[0] == "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    assert kwargs["json"] == {"app_id": "cli_x", "app_secret": "sec"}


def test_get_raises_on_api_error_code() -> None:
    http = MagicMock()
    http.post.return_value = _api_err_resp(99991663, "Invalid app_secret")
    tm = TokenManager(app_id="cli_x", app_secret="sec", http_client=http)
    with pytest.raises(LarkAPIError) as ei:
        tm.get()
    assert ei.value.code == 99991663
    assert "Invalid app_secret" in ei.value.msg


def test_get_raises_on_http_5xx() -> None:
    http = MagicMock()
    http.post.return_value = _http_err_resp(500, "boom")
    tm = TokenManager(app_id="cli_x", app_secret="sec", http_client=http)
    with pytest.raises(LarkAPIError) as ei:
        tm.get()
    assert ei.value.code == 500
    assert "boom" in ei.value.msg


def test_get_raises_on_transport_exception() -> None:
    http = MagicMock()
    http.post.side_effect = RuntimeError("connection refused")
    tm = TokenManager(app_id="cli_x", app_secret="sec", http_client=http)
    with pytest.raises(LarkAPIError) as ei:
        tm.get()
    assert ei.value.code == -1
    assert "connection refused" in ei.value.msg


def test_get_raises_when_payload_missing_token() -> None:
    http = MagicMock()
    http.post.return_value = _ok_resp("", expire=7200)
    http.post.return_value.json.return_value = {"code": 0, "msg": "ok"}
    tm = TokenManager(app_id="cli_x", app_secret="sec", http_client=http)
    with pytest.raises(LarkAPIError, match="malformed payload"):
        tm.get()


def test_close_is_idempotent_and_safe_for_injected_client() -> None:
    http = MagicMock()
    tm = TokenManager(app_id="cli_x", app_secret="sec", http_client=http)
    tm.close()
    tm.close()
    http.close.assert_not_called()


def test_close_owned_client_is_closed_once() -> None:
    """No http_client passed → manager creates one lazily and owns it."""
    tm = TokenManager(app_id="cli_x", app_secret="sec")
    http = MagicMock()
    tm._http = http  # noqa: SLF001 - testing close behavior on owned client
    tm.close()
    http.close.assert_called_once()
    tm.close()
