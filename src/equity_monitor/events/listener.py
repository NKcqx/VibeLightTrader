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
    return reply


def stream_lark_events(
    *,
    cli_path: str = "lark-cli",
    identity: str = "bot",
    event_types: str = "im.message.receive_v1",
) -> Iterator[dict[str, Any]]:
    """Spawn lark-cli event subscribe and yield parsed JSON events forever.

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
        log.info("listener.subprocess_start", cmd=cmd)
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
            log.warning("listener.subprocess_exit", returncode=rc)
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


def run_listener(
    *,
    cfg: AppConfig,
    factory: sessionmaker,
    events: Iterable[dict[str, Any]] | None = None,
) -> None:
    """Long-running entry point. Pair with `equity-monitor run`.

    `events`: optional iterable to inject events for testing; defaults to live
    `stream_lark_events()`. Iterating events forever blocks the caller.
    """
    allowed = cfg.lark.receiver.open_id
    sender = make_text_sender(
        cli_path=cfg.lark.cli_path, identity=cfg.lark.identity
    )

    src = events if events is not None else stream_lark_events(
        cli_path=cfg.lark.cli_path,
        identity=cfg.lark.identity,
    )
    log.info("listener.start", allowed_open_id=allowed)
    for ev in src:
        try:
            dispatch_event(
                ev, factory=factory, allowed_open_id=allowed, send_text=sender
            )
        except Exception:
            log.exception("listener.dispatch_crash")
