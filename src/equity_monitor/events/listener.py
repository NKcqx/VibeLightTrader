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
from typing import Any

import structlog
from sqlalchemy.orm import sessionmaker

from equity_monitor.config import AppConfig
from equity_monitor.events.apply import apply
from equity_monitor.events.grammar import parse

log = structlog.get_logger(__name__)


SendTextFn = Callable[[str, str], str]
"""(text, recipient_open_id) -> message_id."""


def make_text_sender(cli_path: str = "lark-cli", identity: str = "bot") -> SendTextFn:
    """Build a default sender that uses lark-cli +messages-send --text."""

    def _send(text: str, recipient: str) -> str:
        cmd = [
            cli_path,
            "im",
            "+messages-send",
            "--as",
            identity,
            "--user-id",
            recipient,
            "--markdown",
            text,
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
    send_text: SendTextFn,
) -> str | None:
    """Process one Lark event. Returns the reply text actually sent, else None.

    Behavior:
      - Non-im.message.receive_v1 events: ignored (None).
      - Non-text messages: ignored.
      - Sender not in allowlist (when allowlist set): ignored.
      - Recognized command: applied, reply text sent.
      - Unrecognized text: silent ignore (avoid spam loops with self-replies).
    """
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
    try:
        reply = apply(cmd, factory)
    except Exception as e:
        log.exception("listener.apply_failed")
        reply = f"⚠️ 处理失败: {e}"
    try:
        send_text(reply, sender_id)
    except Exception:
        log.exception("listener.reply_failed")
        return None
    log.info(
        "listener.dispatched",
        cmd=type(cmd).__name__,
        text=text[:60],
        sender=sender_id,
    )
    return reply


def stream_lark_events_ws(
    *,
    cli_path: str = "lark-cli",
    identity: str = "bot",
    event_types: str = "im.message.receive_v1",
) -> Iterator[dict[str, Any]]:
    """WebSocket-based event stream (lark-cli event +subscribe).

    NOTE: requires the Lark app to have `im.message.receive_v1` event
    subscription enabled in the Open Platform console. Many ByteDance
    internal lark-cli profiles do NOT have this turned on, in which case
    the WebSocket connects fine but receives 0 events. Use the polling
    backend (`stream_lark_events_polling`) when that's the case.

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
            "--quiet",
        ]
        log.info("listener.ws_subprocess_start", cmd=cmd)
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                if not line:
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
    initial_lookback_seconds: int = 30,
) -> Iterator[dict[str, Any]]:
    """Polling-based event stream — call lark-cli im +chat-messages-list every N s.

    Yields synthetic im.message.receive_v1 events for each NEW text message
    sent by anyone other than the bot itself. Uses message_id dedupe.

    This works without `im.message.receive_v1` event subscription enabled —
    only requires `im:message:readonly` (or `im:message`), which is in the
    default lark-cli scope set.
    """
    seen_ids: set[str] = set()
    # Seed `seen_ids` with current chat tail to avoid re-dispatching old messages
    # at startup. Look back `initial_lookback_seconds` to be safe.
    start_dt = datetime.now(timezone.utc) - timedelta(
        seconds=initial_lookback_seconds
    )

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
            # Skip bot's own messages (sender.id == bot_app_id, sender_type=app)
            if sender.get("id") == bot_app_id:
                continue
            if m.get("msg_type") != "text":
                continue
            new_msgs.append(m)

        # Sort ascending so the earliest message is dispatched first
        new_msgs.sort(key=lambda m: m.get("create_time", ""))
        for m in new_msgs:
            yield _msg_to_event(m, chat_id)

        # On next iteration, only fetch since 5s before the latest seen msg
        # to keep request small without losing late-arriving messages.
        start_dt = datetime.now(timezone.utc) - timedelta(seconds=5)
        time.sleep(poll_interval)


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
    backend: str = "polling",
    poll_interval: int = 10,
) -> None:
    """Long-running entry point. Pair with `equity-monitor run`.

    Args:
        backend: "polling" (default; works without event-subscription scope)
                 or "websocket" (requires app to have im.message.receive_v1
                 enabled in Open Platform console).
        poll_interval: seconds between API polls when backend == "polling".
        events: optional iterable injectable for tests.
    """
    allowed = cfg.lark.receiver.open_id
    sender = make_text_sender(
        cli_path=cfg.lark.cli_path, identity=cfg.lark.identity
    )

    if events is not None:
        src: Iterable[dict[str, Any]] = events
    elif backend == "websocket":
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
    for ev in src:
        try:
            dispatch_event(
                ev, factory=factory, allowed_open_id=allowed, send_text=sender
            )
        except Exception:
            log.exception("listener.dispatch_crash")


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
