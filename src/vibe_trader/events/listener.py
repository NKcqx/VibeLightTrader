"""Event listener: pull DM messages from Lark via HTTP polling, dispatch.

Two layers:
  • :func:`dispatch_event` — pure function, fully unit-testable.
  • :func:`run_listener`   — long-running entry point; pairs with ``vibe-trader run``.

The previous build supported an ``lark-cli event consume`` WebSocket backend
on top of an internal Node binary. That binary is not publicly distributed,
so this rewrite drops WS entirely and ships a single HTTP-polling backend
backed by :class:`vibe_trader.lark.LarkHTTPClient`. Polling needs only
``im:message:readonly`` + ``im:message`` scopes on a public Custom App
(open.feishu.cn) and works out of the box for external users.

Latency: ~3-10s with the adaptive polling interval (fast 3s window after
each new user msg, idle 10s otherwise). Good enough for an interactive
chat-controlled trade tool.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy.orm import sessionmaker

from vibe_trader.config import AppConfig
from vibe_trader.events.apply import apply, apply_chart
from vibe_trader.events.grammar import (
    AddCommand,
    ChartCommand,
    Command,
    HelpCommand,
    ListCommand,
    RemoveCommand,
    ThresholdCommand,
    parse,
)
from vibe_trader.futu_client import FutuClient
from vibe_trader.lark.client import LarkHTTPClient
from vibe_trader.lark.errors import LarkAPIError

log = structlog.get_logger(__name__)


SendTextFn = Callable[[str, str], str]
"""(text, recipient_open_id) -> message_id."""

SendCardFn = Callable[[dict[str, Any], str], str]
"""(card_payload, recipient_open_id) -> message_id."""

SendImageFn = Callable[[Path, str], str]
"""(image_path, recipient_open_id) -> message_id."""

ReplyFn = Callable[[Command, str, str], None]
"""(parsed_cmd, action_text, recipient_open_id) -> None.

Implementations decide whether to send text or a Lark card."""


# ---------------------------------------------------------------------------
# senders
# ---------------------------------------------------------------------------


def make_text_sender(client: LarkHTTPClient) -> SendTextFn:
    """Build a text sender that replies via Lark's text message type.

    Lark's text body supports a markdown-ish subset on the client UI; the
    transport contract is plain text wrapped in ``{"text": "..."}``.
    """

    def _send(text: str, recipient: str) -> str:
        try:
            return client.send_text(text, receive_id=recipient)
        except LarkAPIError as e:
            raise RuntimeError(f"lark text reply failed: {e}") from e

    return _send


def make_card_sender(client: LarkHTTPClient) -> SendCardFn:
    """Build an Interactive Card sender."""

    def _send(card: dict[str, Any], recipient: str) -> str:
        try:
            return client.send_card(card, receive_id=recipient)
        except LarkAPIError as e:
            raise RuntimeError(f"lark card reply failed: {e}") from e

    return _send


def make_image_sender(client: LarkHTTPClient) -> SendImageFn:
    """Build an image sender (uploads + sends in one call)."""

    def _send(path: Path, recipient: str) -> str:
        try:
            return client.send_image(path, receive_id=recipient)
        except LarkAPIError as e:
            raise RuntimeError(f"lark image reply failed: {e}") from e

    return _send


# ---------------------------------------------------------------------------
# card-reply composer (live OpenD enrichment + DB watchlist render)
# ---------------------------------------------------------------------------


def _title_color_for(cmd: Command, count: int) -> tuple[str, str]:
    """Card header title + color from the parsed command."""
    if isinstance(cmd, AddCommand):
        return f"已添加 (共 {count} 个标的)", "green"
    if isinstance(cmd, RemoveCommand):
        return f"已删除 (剩 {count} 个标的)", "orange"
    if isinstance(cmd, ThresholdCommand):
        return f"阈值已更新 (共 {count} 个标的)", "blue"
    if isinstance(cmd, ListCommand):
        return f"监控列表 ({count} 个标的)", "blue"
    return f"监控列表 ({count} 个标的)", "blue"


def make_card_reply(
    *,
    cfg: AppConfig,
    factory: sessionmaker,
    client: Any,  # FutuClient — Any to avoid circular import for tests
    send_text: SendTextFn,
    send_card: SendCardFn,
) -> ReplyFn:
    """Compose live OpenD data + DB watchlist into a Lark card per reply.

    Help replies fall back to plain markdown text (no data lookup needed).
    On enrich/render exceptions, falls back to text reply so the user
    always gets feedback.
    """
    from vibe_trader.events.enrich import build_watchlist_rows, now_utc
    from vibe_trader.reports.render import (
        WatchlistCardRow,
        render_watchlist_card,
    )

    def _reply(cmd: Command, action_text: str, recipient: str) -> None:
        if isinstance(cmd, HelpCommand):
            try:
                card = render_watchlist_card(
                    title="使用指南",
                    action_text=action_text,
                    rows=[],
                    ts=now_utc(),
                    color="purple",
                    footer_md="💬 直接给我发文字即可，无需 @",
                )
                send_card(card, recipient)
            except Exception:
                log.exception("listener.help_card_failed_falling_back_to_text")
                send_text(action_text, recipient)
            return
        try:
            rows, n = build_watchlist_rows(cfg=cfg, factory=factory, client=client)
            title, color = _title_color_for(cmd, n)
            footer = (
                "💡 试试 `添加 US.TSLA 上限260 下限180` · `阈值 US.AAPL 上限290`  ·  发 `帮助` 看完整指令"
                if isinstance(cmd, ListCommand) else ""
            )
            card = render_watchlist_card(
                title=title,
                action_text=action_text,
                rows=rows,
                ts=now_utc(),
                color=color,
                footer_md=footer,
            )
            send_card(card, recipient)
        except Exception:
            log.exception("listener.card_render_failed_falling_back_to_text")
            send_text(action_text, recipient)

    return _reply


# ---------------------------------------------------------------------------
# event extraction
# ---------------------------------------------------------------------------


def _extract_text(event: dict[str, Any]) -> tuple[str | None, str | None]:
    """Pull (message_text, sender_open_id) from a Lark im.message.receive_v1 event.

    Returns (None, None) if the event is the wrong type or text can't be parsed.
    """
    if event.get("event_type") != "im.message.receive_v1":
        return None, None
    body = event.get("event", {})
    msg = body.get("message", {})
    if msg.get("message_type") != "text":
        return None, None  # ignore non-text messages
    content_str = msg.get("content", "")
    try:
        content = json.loads(content_str) if isinstance(content_str, str) else content_str
    except json.JSONDecodeError:
        return None, None
    text = content.get("text", "") if isinstance(content, dict) else ""
    sender = body.get("sender", {})
    sender_id = sender.get("sender_id", {})
    open_id = sender_id.get("open_id")
    return text or None, open_id


def dispatch_event(
    event: dict[str, Any],
    *,
    factory: sessionmaker,
    allowed_open_id: str | None,
    send_text: SendTextFn | None = None,
    reply_fn: ReplyFn | None = None,
    client: FutuClient | None = None,
    send_image: SendImageFn | None = None,
    snapshot_dir: Path | None = None,
) -> str | None:
    """Process one Lark event. Returns the action text replied with, else None.

    Either ``send_text`` (text-only reply) or ``reply_fn`` (rich card reply)
    must be provided. If both are given, ``reply_fn`` wins for watchlist /
    help commands; ChartCommand is dispatched out-of-band — it always uses
    ``send_text`` for the markdown caption and ``send_image`` for the PNG,
    regardless of whether ``reply_fn`` is supplied.
    """
    if reply_fn is None and send_text is None:
        raise ValueError("dispatch_event needs either reply_fn or send_text")

    text, sender_id = _extract_text(event)
    if not text or not sender_id:
        return None
    if allowed_open_id and sender_id != allowed_open_id:
        log.warning(
            "listener.unauthorized", sender=sender_id, allowed=allowed_open_id
        )
        return None
    cmd = parse(text)
    if cmd is None:
        log.debug("listener.unrecognized", text=text[:80])
        return None  # silent — user's casual chat shouldn't trigger replies

    if isinstance(cmd, ChartCommand):
        if client is None or send_image is None:
            action_text = "⚠️ /chart 当前不可用 (OpenD 未连接或未启用图片发送)。"
            if send_text is not None:
                try:
                    send_text(action_text, sender_id)
                except Exception:
                    log.exception("listener.chart_fallback_send_failed")
            return action_text
        try:
            action_text, payload = apply_chart(
                cmd, factory, client=client, snapshot_dir=snapshot_dir
            )
        except Exception as e:
            log.exception("listener.chart_apply_failed")
            action_text = f"⚠️ /chart 失败: {e}"
            if send_text is not None:
                with _safe():
                    send_text(action_text, sender_id)
            return action_text
        if send_text is not None:
            try:
                send_text(action_text, sender_id)
            except Exception:
                log.exception("listener.chart_text_send_failed")
        try:
            send_image(payload.image_path, sender_id)
        except Exception:
            log.exception("listener.chart_image_send_failed")
        log.info(
            "listener.dispatched",
            cmd="ChartCommand",
            code=cmd.code,
            freq=cmd.freq,
            sender=sender_id,
        )
        return action_text

    try:
        action_text = apply(cmd, factory)
    except Exception as e:
        log.exception("listener.apply_failed")
        action_text = f"⚠️ 处理失败: {e}"
    try:
        if reply_fn is not None:
            reply_fn(cmd, action_text, sender_id)
        else:
            assert send_text is not None
            send_text(action_text, sender_id)
    except Exception:
        log.exception("listener.reply_failed")
        return None
    log.info(
        "listener.dispatched",
        cmd=type(cmd).__name__,
        text=text[:60],
        sender=sender_id,
    )
    return action_text


class _safe:
    """Trivial swallow-everything context manager for best-effort blocks."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return True  # swallow


# ---------------------------------------------------------------------------
# polling event source (HTTP, /im/v1/messages)
# ---------------------------------------------------------------------------


def stream_lark_events_polling(
    *,
    http_client: LarkHTTPClient,
    chat_id: str,
    bot_open_id: str | None,
    poll_interval: int = 10,
    fast_interval: int = 3,
    fast_window_seconds: int = 60,
    initial_lookback_seconds: int = 30,
) -> Iterator[dict[str, Any]]:
    """Adaptive HTTP polling of a single p2p chat.

    Behaviour mirrors the previous lark-cli polling implementation:

      - Idle baseline: poll every ``poll_interval`` s (default 10s).
      - On a new user message, switch to ``fast_interval`` (default 3s) for
        the next ``fast_window_seconds`` (default 60s) so a follow-up
        command is noticed near-instantly.

    Yields synthetic ``im.message.receive_v1`` events (same shape the WS
    webhook produces) so :func:`dispatch_event` doesn't need to know which
    backend produced the event. Dedupes on ``message_id``; skips the bot's
    own messages by ``sender.id == bot_open_id``.
    """
    seen_ids: set[str] = set()
    start_dt = datetime.now(timezone.utc) - timedelta(seconds=initial_lookback_seconds)
    last_user_msg_at: datetime | None = None

    while True:
        try:
            data = http_client.list_chat_messages(
                chat_id=chat_id,
                start_time=int(start_dt.timestamp()),
                page_size=20,
                sort_type="ByCreateTimeDesc",
            )
        except Exception:
            log.exception("listener.poll_fetch_failed")
            time.sleep(poll_interval)
            continue

        messages = data.get("items") or []

        new_msgs: list[dict[str, Any]] = []
        for m in messages:
            mid = m.get("message_id")
            if not mid or mid in seen_ids:
                continue
            seen_ids.add(mid)
            sender = m.get("sender", {})
            if bot_open_id and sender.get("id") == bot_open_id:
                continue
            if m.get("msg_type") != "text":
                continue
            new_msgs.append(m)

        new_msgs.sort(key=lambda m: m.get("create_time", ""))
        for m in new_msgs:
            yield _msg_to_event(m, chat_id)

        if new_msgs:
            last_user_msg_at = datetime.now(timezone.utc)

        now = datetime.now(timezone.utc)
        in_fast_window = (
            last_user_msg_at is not None
            and (now - last_user_msg_at).total_seconds() < fast_window_seconds
        )
        sleep_for = fast_interval if in_fast_window else poll_interval

        if len(seen_ids) > 2000:
            seen_ids.clear()

        start_dt = now - timedelta(seconds=5)
        time.sleep(sleep_for)


def _msg_to_event(msg: dict[str, Any], chat_id: str) -> dict[str, Any]:
    """Wrap a /im/v1/messages row into an im.message.receive_v1-shaped event.

    Two transformations:
      • ``msg.body.content`` from list-messages is a JSON string already
        (``'{"text":"..."}'``); we preserve that shape since the webhook
        delivers the same envelope and ``_extract_text`` parses it.
      • ``msg.sender.id`` (open_id) → event.sender.sender_id.open_id.
    """
    body = msg.get("body") or {}
    content = body.get("content") or msg.get("content") or ""
    if isinstance(content, dict):
        content = json.dumps(content)
    elif not isinstance(content, str):
        content = str(content)
    return {
        "event_type": "im.message.receive_v1",
        "event": {
            "sender": {
                "sender_id": {
                    "open_id": (msg.get("sender") or {}).get("id", ""),
                },
            },
            "message": {
                "message_id": msg.get("message_id", ""),
                "chat_id": chat_id,
                "chat_type": "p2p",
                "message_type": msg.get("msg_type", "text"),
                "content": content,
            },
        },
    }


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------


def _build_lark_client_or_die(cfg: AppConfig) -> LarkHTTPClient:
    """Resolve the LarkHTTPClient required for the listener.

    Unlike cron jobs (which gracefully no-op without Lark transport), the
    listener fundamentally requires Lark — fail loudly so the user knows
    to configure ``lark.app_id`` and the secret env var.
    """
    import os

    if not cfg.lark.app_id:
        raise RuntimeError(
            "listener requires lark.app_id in settings.yaml. Create a "
            "Custom App at https://open.feishu.cn → 应用配置 → 凭证与基础信息, "
            "then set lark.app_id and export the matching secret as "
            f"{cfg.lark.app_secret_env}."
        )
    secret = os.environ.get(cfg.lark.app_secret_env, "").strip()
    if not secret:
        raise RuntimeError(
            f"listener requires the env var {cfg.lark.app_secret_env!r} "
            "to hold the Custom App app_secret. Export it and retry."
        )
    from vibe_trader.lark.auth import TokenManager

    tm = TokenManager(
        app_id=cfg.lark.app_id, app_secret=secret, base_url=cfg.lark.base_url
    )
    return LarkHTTPClient(tm, base_url=cfg.lark.base_url)


def run_listener(
    *,
    cfg: AppConfig,
    factory: sessionmaker,
    events: Iterable[dict[str, Any]] | None = None,
    backend: str = "polling",
    poll_interval: int = 10,
    rich_cards: bool = True,
    http_client: LarkHTTPClient | None = None,
) -> None:
    """Long-running entry point. Pair with ``vibe-trader run``.

    Args:
        backend: Reserved — the only supported backend is ``"polling"``.
            For backwards compatibility, ``"websocket"`` is accepted but
            falls back to polling with a warning (the old WS backend
            depended on an internal Node CLI that's no longer used).
        poll_interval: idle-state polling interval in seconds.
        rich_cards: if True (default), replies are Lark Interactive Cards
            enriched with live OpenD price + RSI/MACD/BOLL diagnostics.
            Set False for plain markdown text replies.
        events: optional iterable of pre-built events; injectable for tests.
        http_client: optional pre-built LarkHTTPClient; injectable for
            tests. When None and ``events`` is also None, one is built
            from cfg.
    """
    allowed = cfg.lark.receiver.open_id

    if backend not in ("polling", "websocket"):
        raise ValueError(f"unknown backend: {backend!r}")
    if backend == "websocket":
        log.warning(
            "listener.ws_backend_deprecated_falling_back_to_polling",
            note="HTTP-only build; the previous lark-cli WS subprocess is gone.",
        )
        backend = "polling"

    # Build (or accept) the LarkHTTPClient that powers all sends + the
    # polling source. Tests can inject either ``http_client`` (to observe
    # send calls) or ``events`` (to skip the polling source). When neither
    # is given we build the production client from cfg.
    using_injected_events = events is not None
    we_built_client = False
    if http_client is None and not using_injected_events:
        http_client = _build_lark_client_or_die(cfg)
        we_built_client = True

    if http_client is not None:
        text_sender: SendTextFn = make_text_sender(http_client)
        card_sender: SendCardFn = make_card_sender(http_client)
        image_sender: SendImageFn | None = (
            make_image_sender(http_client) if rich_cards else None
        )
    else:
        # Tests with `events=...` and no `http_client=...`: senders are
        # no-ops so the dispatch path still has callables to invoke.
        def _noop_text(text: str, recipient: str) -> str:
            return ""

        def _noop_card(card: dict[str, Any], recipient: str) -> str:
            return ""

        text_sender = _noop_text
        card_sender = _noop_card
        image_sender = None

    futu_client: Any = None
    if rich_cards:
        from vibe_trader.futu_client import OpenDClient

        try:
            futu_client = OpenDClient(cfg.opend.host, cfg.opend.port)
        except Exception:
            log.exception("listener.opend_init_failed_falling_back_to_text")
            futu_client = None

    reply_fn: ReplyFn
    if rich_cards and futu_client is not None:
        reply_fn = make_card_reply(
            cfg=cfg,
            factory=factory,
            client=futu_client,
            send_text=text_sender,
            send_card=card_sender,
        )
        log.info("listener.replies_mode", mode="card")
    else:
        def _text_reply(cmd: Command, action_text: str, recipient: str) -> None:
            text_sender(action_text, recipient)

        reply_fn = _text_reply
        log.info("listener.replies_mode", mode="text")

    if events is not None:
        src: Iterable[dict[str, Any]] = events
    else:
        if not allowed:
            raise RuntimeError(
                "listener needs lark.receiver.open_id in settings.yaml so it "
                "knows which p2p chat to poll."
            )
        assert http_client is not None  # set in the not-using_injected_events branch
        try:
            chat_id = http_client.resolve_p2p_chat_id(
                allowed, init_text="🟢 listener online"
            )
        except LarkAPIError as e:
            raise RuntimeError(
                f"resolve_p2p_chat_id failed for receiver {allowed!r}: {e}"
            ) from e
        log.info(
            "listener.polling_resolved",
            chat_id=chat_id,
            bot_open_id=allowed,  # for now we skip bot's own msgs by allowed-only filter
            poll_interval=poll_interval,
        )
        src = stream_lark_events_polling(
            http_client=http_client,
            chat_id=chat_id,
            bot_open_id=None,  # bot replies have a different open_id; allowed-filter
                               # in dispatch_event already gates on the user's id, so
                               # we don't need an explicit "skip my own msgs" filter.
            poll_interval=poll_interval,
        )

    log.info("listener.start", backend=backend, allowed_open_id=allowed)
    try:
        for ev in src:
            try:
                dispatch_event(
                    ev,
                    factory=factory,
                    allowed_open_id=allowed,
                    reply_fn=reply_fn,
                    send_text=text_sender,
                    client=futu_client,
                    send_image=image_sender,
                    snapshot_dir=Path("var/snapshots").resolve(),
                )
            except Exception:
                log.exception("listener.dispatch_crash")
    finally:
        if futu_client is not None:
            with _safe():
                futu_client.close()
        if we_built_client and http_client is not None:
            with _safe():
                http_client.close()
