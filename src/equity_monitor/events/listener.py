"""Event listener: lark-cli WebSocket subscription → command dispatch.

Two layers:
  • `dispatch_event(event_dict, ...)` - pure function, fully unit-testable.
  • `run_listener(...)` - spawns the lark-cli subprocess and iterates NDJSON.

Run via: `equity-monitor listen` (long-running). Pair with `equity-monitor run`
in a tmux pane.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy.orm import sessionmaker

from equity_monitor.config import AppConfig
from equity_monitor.events.apply import apply, apply_chart
from equity_monitor.events.grammar import (
    AddCommand,
    ChartCommand,
    Command,
    HelpCommand,
    ListCommand,
    RemoveCommand,
    ThresholdCommand,
    parse,
)
from equity_monitor.futu_client import FutuClient

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


def make_text_sender(cli_path: str = "lark-cli", identity: str = "bot") -> SendTextFn:
    """Build a default text sender via lark-cli +messages-send --markdown."""

    def _send(text: str, recipient: str) -> str:
        cmd = [
            cli_path, "im", "+messages-send",
            "--as", identity,
            "--user-id", recipient,
            "--markdown", text,
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=15, check=False
        )
        if result.returncode != 0:
            raise RuntimeError(f"lark-cli reply failed: {result.stderr.strip()}")
        try:
            j = json.loads(result.stdout)
            return str(j.get("data", {}).get("message_id", ""))
        except json.JSONDecodeError:
            return result.stdout.strip()

    return _send


def make_card_sender(cli_path: str = "lark-cli", identity: str = "bot") -> SendCardFn:
    """Build a default card sender via lark-cli +messages-send --content / interactive."""

    def _send(card: dict[str, Any], recipient: str) -> str:
        cmd = [
            cli_path, "im", "+messages-send",
            "--as", identity,
            "--user-id", recipient,
            "--msg-type", "interactive",
            "--content", json.dumps(card, ensure_ascii=False),
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=15, check=False
        )
        if result.returncode != 0:
            raise RuntimeError(f"lark-cli card send failed: {result.stderr.strip()}")
        try:
            j = json.loads(result.stdout)
            return str(j.get("data", {}).get("message_id", ""))
        except json.JSONDecodeError:
            return result.stdout.strip()

    return _send


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
    from equity_monitor.events.enrich import build_watchlist_rows, now_utc
    from equity_monitor.reports.render import (
        render_watchlist_card,
        WatchlistCardRow,
    )

    def _reply(cmd: Command, action_text: str, recipient: str) -> None:
        if isinstance(cmd, HelpCommand):
            # Render help as a card too (no OpenD lookup needed).
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

    Either `send_text` (text-only reply) or `reply_fn` (rich card reply) must
    be provided. If both are given, `reply_fn` wins for watchlist/help commands.
    The text-only path is kept for tests / minimal deployments without OpenD.

    Note: ChartCommand is dispatched out-of-band — it always uses `send_text`
    for the markdown caption and `send_image` for the PNG, regardless of
    whether `reply_fn` is supplied. The card path is for the watchlist
    commands only.
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


def stream_lark_events_ws(
    *,
    cli_path: str = "lark-cli",
    identity: str = "bot",
    event_types: str = "im.message.receive_v1",
) -> Iterator[dict[str, Any]]:
    """WebSocket-based event stream (lark-cli event +subscribe).

    Requires the bot's Lark app to have `im.message.receive_v1` registered
    under "事件与回调" in the Open Platform console (long-connection mode).

    Caveat (observed): if multiple `lark-cli event +subscribe` processes
    bind the same bot at once, the Lark backend round-robins events
    across them, so an individual instance may appear silent. This iterator
    is intended to be the SOLE subscriber per bot — kill any stray
    subscribers (`pgrep -f 'lark-cli event'`) before relying on it.

    Yields one parsed event dict per NDJSON line on stdout. Status lines
    (e.g. "Connecting…", "Connected.", "[SDK Info]") are skipped quietly.
    Auto-restarts on subprocess crash with exponential backoff.
    """
    backoff = 1.0
    while True:
        cmd = [
            cli_path,
            "event",
            "+subscribe",
            "--as",
            identity,
            "--event-types",
            event_types,
        ]
        log.info("listener.ws_subprocess_start", cmd=cmd)
        # Stderr → stdout so [SDK Info]/status lines don't backpressure;
        # filter them out by leading-{ check below. Requires lark-cli
        # >= 1.0.23 (older builds suppress NDJSON when stdout is a pipe).
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.strip()
                if not line:
                    continue
                if not line.startswith("{"):
                    log.debug("listener.ws_status", line=line[:200])
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as e:
                    log.warning("listener.bad_ndjson", error=str(e), line=line[:200])
            rc = proc.wait()
            log.warning("listener.ws_subprocess_exit", returncode=rc)
        finally:
            with _safe():
                proc.kill()
                proc.wait(timeout=2)
        time.sleep(backoff)
        backoff = min(backoff * 2.0, 60.0)


class _safe:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return True  # swallow


def _resolve_p2p_chat_id(
    cli_path: str, identity: str, recipient_open_id: str
) -> str:
    """Resolve (or create) the bot⇆user p2p chat by sending a benign init ping.

    Lark p2p chats are implicit: no "create chat" API needed; sending a
    message returns the chat_id which is stable thereafter.
    """
    cmd = [
        cli_path, "im", "+messages-send",
        "--as", identity,
        "--user-id", recipient_open_id,
        "--text", "🟢 listener online",
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=15, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"failed to resolve p2p chat_id: {result.stderr.strip()}"
        )
    j = json.loads(result.stdout)
    chat_id = j.get("data", {}).get("chat_id")
    if not chat_id:
        raise RuntimeError(f"no chat_id in send response: {result.stdout[:200]}")
    return chat_id


def stream_lark_events_polling(
    *,
    cli_path: str,
    identity: str,
    chat_id: str,
    bot_app_id: str,
    poll_interval: int = 10,
    fast_interval: int = 3,
    fast_window_seconds: int = 60,
    initial_lookback_seconds: int = 30,
) -> Iterator[dict[str, Any]]:
    """Adaptive polling — fast for `fast_window_seconds` after each new user msg.

    - Idle baseline: poll every `poll_interval` s (default 10s).
    - On a new user message, switch to `fast_interval` (default 3s) for the
      next `fast_window_seconds` (default 60s) so a follow-up command is
      noticed near-instantly.

    Yields synthetic im.message.receive_v1 events for each NEW user-sent text
    message. Uses message_id dedupe and skips bot's own messages by app_id.
    Requires only `im:message:readonly` scope (no event-subscription config).
    """
    seen_ids: set[str] = set()
    start_dt = datetime.now(timezone.utc) - timedelta(seconds=initial_lookback_seconds)
    last_user_msg_at: datetime | None = None

    while True:
        try:
            messages = _fetch_messages(
                cli_path=cli_path,
                identity=identity,
                chat_id=chat_id,
                start_iso=start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
        except Exception:
            log.exception("listener.poll_fetch_failed")
            time.sleep(poll_interval)
            continue

        new_msgs: list[dict[str, Any]] = []
        for m in messages:
            mid = m.get("message_id")
            if not mid or mid in seen_ids:
                continue
            seen_ids.add(mid)
            sender = m.get("sender", {})
            if sender.get("id") == bot_app_id:
                continue
            if m.get("msg_type") != "text":
                continue
            new_msgs.append(m)

        new_msgs.sort(key=lambda m: m.get("create_time", ""))
        for m in new_msgs:
            yield _msg_to_event(m, chat_id)

        if new_msgs:
            last_user_msg_at = datetime.now(timezone.utc)

        # Decide next sleep duration based on activity
        now = datetime.now(timezone.utc)
        in_fast_window = (
            last_user_msg_at is not None
            and (now - last_user_msg_at).total_seconds() < fast_window_seconds
        )
        sleep_for = fast_interval if in_fast_window else poll_interval

        # Trim seen_ids to bounded size (~last 1000 msgs is plenty)
        if len(seen_ids) > 2000:
            # Keep the most recent half — order isn't tracked, so just clear and reseed
            seen_ids.clear()

        start_dt = now - timedelta(seconds=5)
        time.sleep(sleep_for)


def _fetch_messages(
    *, cli_path: str, identity: str, chat_id: str, start_iso: str
) -> list[dict[str, Any]]:
    cmd = [
        cli_path, "im", "+chat-messages-list",
        "--as", identity,
        "--chat-id", chat_id,
        "--start", start_iso,
        "--sort", "desc",
        "--page-size", "20",
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=20, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"lark-cli list failed: {result.stderr.strip()[:300]}")
    payload = json.loads(result.stdout)
    if not payload.get("ok", False):
        raise RuntimeError(f"lark-cli list not ok: {payload}")
    return payload.get("data", {}).get("messages", []) or []


def _msg_to_event(msg: dict[str, Any], chat_id: str) -> dict[str, Any]:
    """Wrap a /im/v1/messages row into an im.message.receive_v1-shaped event.

    Note `content` for text msgs from this API is the bare text, while the
    receive_v1 webhook delivers it as JSON-encoded `{"text":"..."}`. We
    re-wrap to match `dispatch_event`/`_extract_text`'s expectations.
    """
    text_body = msg.get("content", "")
    return {
        "event_type": "im.message.receive_v1",
        "event": {
            "sender": {
                "sender_id": {
                    "open_id": msg.get("sender", {}).get("id", ""),
                },
            },
            "message": {
                "message_id": msg.get("message_id", ""),
                "chat_id": chat_id,
                "chat_type": "p2p",
                "message_type": msg.get("msg_type", "text"),
                "content": json.dumps({"text": text_body}),
            },
        },
    }


def run_listener(
    *,
    cfg: AppConfig,
    factory: sessionmaker,
    events: Iterable[dict[str, Any]] | None = None,
    backend: str = "websocket",
    poll_interval: int = 10,
    rich_cards: bool = True,
) -> None:
    """Long-running entry point. Pair with `equity-monitor run`.

    Args:
        backend: "websocket" (default; uses lark-cli event +subscribe long
                 connection — requires `im.message.receive_v1` registered
                 in Open Platform "事件与回调") or "polling" (fallback that
                 reads chat history; works with `im:message:readonly` only).
        poll_interval: seconds between API polls when backend == "polling".
        rich_cards: if True (default), replies are Lark Interactive Cards
                    enriched with live OpenD price + RSI/MACD/BOLL diagnostics.
                    Set False for plain markdown text replies (e.g. when OpenD
                    is unavailable).
        events: optional iterable injectable for tests.
    """
    allowed = cfg.lark.receiver.open_id
    text_sender = make_text_sender(
        cli_path=cfg.lark.cli_path, identity=cfg.lark.identity
    )

    image_sender: SendImageFn | None = None
    if rich_cards:
        from equity_monitor.scheduler.jobs import (
            _make_default_image_sender as _mk_img,
        )

        def _img_send(path: Path, recipient: str) -> str:
            return _mk_img(
                cli_path=cfg.lark.cli_path, identity=cfg.lark.identity
            )(path, recipient, cfg.lark.receiver.type)

        image_sender = _img_send

    reply_fn: ReplyFn
    futu_client: Any = None
    if rich_cards:
        from equity_monitor.futu_client import OpenDClient

        try:
            futu_client = OpenDClient(cfg.opend.host, cfg.opend.port)
        except Exception:
            log.exception("listener.opend_init_failed_falling_back_to_text")
            futu_client = None

    if rich_cards and futu_client is not None:
        card_sender = make_card_sender(
            cli_path=cfg.lark.cli_path, identity=cfg.lark.identity
        )
        reply_fn = make_card_reply(
            cfg=cfg,
            factory=factory,
            client=futu_client,
            send_text=text_sender,
            send_card=card_sender,
        )
        log.info("listener.replies_mode", mode="card")
    else:
        # Wrap text sender into ReplyFn signature (cmd ignored).
        def _text_reply(cmd: Command, action_text: str, recipient: str) -> None:
            text_sender(action_text, recipient)

        reply_fn = _text_reply
        log.info("listener.replies_mode", mode="text")

    if events is not None:
        src: Iterable[dict[str, Any]] = events
    elif backend == "websocket":
        # Send a one-shot "online" ping so the user knows the listener is
        # actually up before they start typing commands. Also doubles as a
        # smoke test for outbound credentials.
        if allowed:
            with _safe():
                _resolve_p2p_chat_id(cfg.lark.cli_path, cfg.lark.identity, allowed)
                log.info("listener.online_ping_sent", recipient=allowed)
        src = stream_lark_events_ws(
            cli_path=cfg.lark.cli_path,
            identity=cfg.lark.identity,
        )
    elif backend == "polling":
        if not allowed:
            raise RuntimeError(
                "polling backend needs lark.receiver.open_id in settings.yaml"
            )
        bot_app_id = _read_bot_app_id(cfg.lark.cli_path)
        chat_id = _resolve_p2p_chat_id(cfg.lark.cli_path, cfg.lark.identity, allowed)
        log.info(
            "listener.polling_resolved",
            chat_id=chat_id,
            bot_app_id=bot_app_id,
            poll_interval=poll_interval,
        )
        src = stream_lark_events_polling(
            cli_path=cfg.lark.cli_path,
            identity=cfg.lark.identity,
            chat_id=chat_id,
            bot_app_id=bot_app_id,
            poll_interval=poll_interval,
        )
    else:
        raise ValueError(f"unknown backend: {backend!r}")

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


def _read_bot_app_id(cli_path: str) -> str:
    """Get current bot app_id via `lark-cli auth scopes --format json`."""
    result = subprocess.run(
        [cli_path, "auth", "scopes", "--format", "json"],
        capture_output=True, text=True, timeout=10, check=False,
    )
    try:
        j = json.loads(result.stdout)
    except json.JSONDecodeError:
        # Fallback: pretty format writes "App ID: cli_xxx" to stderr.
        import re

        m = re.search(r"App\s*ID:\s*(\S+)", result.stderr)
        return m.group(1) if m else ""
    return str(j.get("appId", "") or j.get("app_id", ""))
