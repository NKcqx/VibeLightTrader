"""Cursor Agent CLI as an LLMClient.

Adapts the locally-installed `cursor-agent` CLI (from
https://cursor.com/install) to the LLMClient Protocol so it can be
plugged into LLMStrategy without any other changes. The user authenticates
once with `cursor-agent login`; subsequent CLI invocations consume the
user's IDE Pro/Max subscription quota — no separate API key, no extra
billing.

Why this matters: when the user has Cursor / Claude / Codex subscriptions
but no programmatic LLM API key, this is how we let the strategy layer
"borrow" the IDE subscription. The CLI's `--print --output-format json`
mode produces a stable JSON envelope ({"result": "...", "is_error": ...,
"usage": {...}}) that we map back into LLMResponse.

Concurrency: subprocess is thread-safe; multiple symbols can decide in
parallel as long as the user's account allows concurrent agent runs.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any

import structlog

from equity_monitor.llm.client import (
    LLMError,
    LLMHTTPError,
    LLMParseError,
    LLMResponse,
    LLMTimeoutError,
    Message,
)

log = structlog.get_logger(__name__)


# Headless-mode flags we always set:
# --print               : non-interactive, scriptable
# --output-format json  : single-line JSON envelope on stdout
# --trust               : don't prompt to trust the workspace
# --force               : auto-allow tool calls (Read/Shell/Edit) — safe
#                         here because the prompt is constrained to "output
#                         JSON" and the agent runs in our repo, but the
#                         caller controls whether to pass it (see below).
# --workspace <path>    : where the agent's tools resolve relative paths
_DEFAULT_FLAGS = ("--print", "--output-format", "json", "--trust")


@dataclass
class CursorAgentClient:
    """LLMClient backed by the `cursor-agent` binary.

    Construction parameters:

    - `model`: passed via `--model`. If empty, the agent uses the user's
      account default (configured in cursor.com dashboard). Common values:
      "sonnet-4", "sonnet-4-thinking", "gpt-5", "auto".
    - `workspace`: directory the agent treats as cwd for its file tools.
      Should be the equity-monitor repo root (so the receiver can read
      packets, audit logs, README via relative paths if it wants).
    - `binary`: override for the cursor-agent executable path. Defaults
      to looking up "cursor-agent" on PATH.
    - `extra_flags`: appended to every invocation. Use to pass e.g.
      `--mode plan` for read-only runs, or `--force`/`--yolo` to allow
      shell/edit tools (the default does NOT include `--force` — the
      receiver gets read-only Tools, which is what we want for a pure
      "decide and emit JSON" task).
    """

    model: str = ""
    workspace: str | None = None
    binary: str = "cursor-agent"
    extra_flags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def name(self) -> str:
        return f"cursor-agent:{self.model or 'default'}"

    def chat(
        self,
        messages: list[Message],
        *,
        max_tokens: int,
        temperature: float,
        timeout_s: float,
    ) -> LLMResponse:
        """Spawn `cursor-agent -p '<prompt>'` and adapt the result.

        max_tokens and temperature are accepted for protocol conformance
        but currently NOT plumbed through — the cursor-agent CLI doesn't
        expose those knobs (the IDE settings govern). Documented here so
        nobody mistakes the silence for a bug.
        """
        # Normalise the binary path early so a missing install fails with
        # a clear error instead of FileNotFoundError mid-subprocess.
        binary_path = self._resolve_binary()

        # cursor-agent takes one positional `prompt` arg. We concatenate
        # system + user with a separator the receiver-Claude can parse.
        # Keep the system block first because the agent reads top-down.
        prompt = self._concat_messages(messages)

        args: list[str] = [binary_path, *_DEFAULT_FLAGS]
        if self.model:
            args.extend(["--model", self.model])
        if self.workspace:
            args.extend(["--workspace", self.workspace])
        args.extend(self.extra_flags)
        args.extend(["-p", prompt])

        log.debug(
            "cursor_agent.invoke",
            model=self.model or "default",
            workspace=self.workspace,
            timeout_s=timeout_s,
            prompt_len=len(prompt),
        )

        try:
            proc = subprocess.run(
                args,
                timeout=timeout_s,
                capture_output=True,
                text=True,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            raise LLMTimeoutError(
                f"cursor-agent did not finish within {timeout_s}s",
                provider="cursor-agent",
            ) from e
        except FileNotFoundError as e:  # pragma: no cover — caught by _resolve_binary
            raise LLMError(
                f"cursor-agent binary not found at {binary_path!r}; "
                f"install with: curl https://cursor.com/install | bash",
                provider="cursor-agent",
            ) from e

        if proc.returncode != 0:
            # Non-zero exit before JSON envelope — usually auth, network,
            # or "not logged in". Stderr typically carries a one-liner.
            raise LLMHTTPError(
                f"cursor-agent exit={proc.returncode}: "
                f"{(proc.stderr or proc.stdout or '').strip()[:300]}",
                provider="cursor-agent",
                status_code=proc.returncode,
                body=proc.stderr or proc.stdout,
            )

        return self._parse_envelope(proc.stdout)

    # -------------------- internals --------------------

    def _resolve_binary(self) -> str:
        if self.binary == "cursor-agent" or "/" not in self.binary:
            found = shutil.which(self.binary)
            if not found:
                raise LLMError(
                    f"`{self.binary}` not on PATH; "
                    f"install with: curl https://cursor.com/install | bash",
                    provider="cursor-agent",
                )
            return found
        return self.binary

    @staticmethod
    def _concat_messages(messages: list[Message]) -> str:
        """Flatten OpenAI-style messages into one prompt string.

        cursor-agent doesn't have a native system role; we mark it
        explicitly so the receiver knows which voice is which.
        """
        parts: list[str] = []
        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            if role == "system":
                parts.append(f"[System]\n{content}")
            elif role == "assistant":
                parts.append(f"[Assistant prior turn]\n{content}")
            else:
                parts.append(content)
        return "\n\n".join(parts)

    def _parse_envelope(self, stdout: str) -> LLMResponse:
        """Parse cursor-agent's `--output-format json` line.

        Envelope shape (verified empirically against 2026.05.01 release):
            {
              "type": "result",
              "subtype": "success" | "error",
              "is_error": bool,
              "duration_ms": int,
              "duration_api_ms": int,
              "result": "<assistant text>",
              "session_id": "...",
              "request_id": "...",
              "usage": {
                "inputTokens": int, "outputTokens": int,
                "cacheReadTokens": int, "cacheWriteTokens": int
              }
            }
        """
        stdout_stripped = stdout.strip()
        if not stdout_stripped:
            raise LLMError(
                "cursor-agent returned empty stdout",
                provider="cursor-agent",
            )

        # cursor-agent sometimes emits ANSI control sequences before the
        # JSON line. Find the first '{' to be safe.
        brace = stdout_stripped.find("{")
        if brace < 0:
            raise LLMParseError(
                f"cursor-agent stdout has no JSON envelope: "
                f"{stdout_stripped[:200]!r}",
                provider="cursor-agent",
            )

        try:
            data = json.loads(stdout_stripped[brace:])
        except json.JSONDecodeError as e:
            raise LLMParseError(
                f"cursor-agent stdout is not valid JSON: {e}; "
                f"first 200 chars: {stdout_stripped[brace : brace + 200]!r}",
                provider="cursor-agent",
            ) from e

        if data.get("is_error"):
            raise LLMHTTPError(
                f"cursor-agent reported error subtype="
                f"{data.get('subtype')!r}: "
                f"{str(data.get('result', ''))[:300]}",
                provider="cursor-agent",
                body=stdout_stripped[brace:],
            )

        text = data.get("result")
        if not isinstance(text, str):
            raise LLMParseError(
                f"cursor-agent envelope missing 'result' string field; "
                f"got: {data!r}",
                provider="cursor-agent",
            )

        usage = data.get("usage") or {}
        return LLMResponse(
            text=text.strip(),
            prompt_tokens=_safe_int(usage.get("inputTokens")),
            completion_tokens=_safe_int(usage.get("outputTokens")),
            finish_reason=data.get("subtype"),
            raw=data,
        )


def _safe_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
