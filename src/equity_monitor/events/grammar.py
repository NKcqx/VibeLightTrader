"""Lark message → watchlist command parser.

Supports both slash-style commands and natural Chinese/English phrasing.
All grammars yield one of the typed `Command` dataclasses or `None`.

Design rule: keep this module pure (no DB, no network, no I/O). The result
is fed to `apply.py` which mutates state.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class AddCommand:
    code: str
    upper: float | None = None
    lower: float | None = None
    name: str | None = None


@dataclass(frozen=True)
class RemoveCommand:
    code: str


@dataclass(frozen=True)
class ListCommand:
    pass


@dataclass(frozen=True)
class ThresholdCommand:
    code: str
    upper: float | None = None
    lower: float | None = None


@dataclass(frozen=True)
class HelpCommand:
    pass


@dataclass(frozen=True)
class ChartCommand:
    code: str
    freq: str = "60m"


ALLOWED_CHART_FREQS: frozenset[str] = frozenset({"5m", "15m", "30m", "60m", "D", "W"})


Command = (
    AddCommand
    | RemoveCommand
    | ListCommand
    | ThresholdCommand
    | HelpCommand
    | ChartCommand
)


# US.AAPL, HK.0700, AAPL → captured then uppercased by _normalize_code
_CODE_RE = re.compile(
    r"\b(US\.[A-Za-z]{1,5}|HK\.\d{4,5}|[A-Za-z]{1,5})\b"
)
# float or int with optional sign
_NUM = r"\d+(?:\.\d+)?"


def parse(text: str) -> Command | None:
    """Parse a Lark message body into a Command, or None if unrecognized.

    Tolerant to mentions, leading/trailing whitespace, and full-width punctuation.
    """
    if not text:
        return None
    s = text.strip()
    # Strip Lark @mention prefix like '@小助手 ' or '@bot_name ' — just drop the @<token>.
    s = re.sub(r"^@\S+\s+", "", s)
    # Normalize full-width chars common in Chinese input
    s = (
        s.replace("=", "=")
        .replace(",", ",")
        .replace(":", ":")
        .replace(" ", " ")
    )
    low = s.lower()

    # ---- HELP ------------------------------------------------------------
    if low in {"/help", "help", "帮助", "?", "？"}:
        return HelpCommand()

    # ---- LIST ------------------------------------------------------------
    if low in {"/list", "list", "看监控", "看列表", "监控列表", "列表", "ls"}:
        return ListCommand()

    # ---- ADD -------------------------------------------------------------
    add = _try_parse_add(s)
    if add is not None:
        return add

    # ---- REMOVE ----------------------------------------------------------
    rm = _try_parse_remove(s)
    if rm is not None:
        return rm

    # ---- THRESHOLD -------------------------------------------------------
    th = _try_parse_threshold(s)
    if th is not None:
        return th

    ch = _try_parse_chart(s)
    if ch is not None:
        return ch

    return None


def _normalize_code(raw: str) -> str:
    """`aapl` → `US.AAPL` if no market prefix present."""
    raw = raw.strip().upper()
    if "." in raw:
        return raw
    return f"US.{raw}"


def _extract_thresholds(s: str) -> tuple[float | None, float | None]:
    """Find upper/lower from a string. Supports many phrasings."""
    upper: float | None = None
    lower: float | None = None

    # English keyword forms: upper=200, lower=165, ub=200, hi=200, lo=165
    m = re.search(rf"(?:upper|ub|hi|high|max)\s*=?\s*({_NUM})", s, re.I)
    if m:
        upper = float(m.group(1))
    m = re.search(rf"(?:lower|lb|lo|low|min)\s*=?\s*({_NUM})", s, re.I)
    if m:
        lower = float(m.group(1))

    # Chinese phrases: 上限200 / 上限 200 / 上限=200
    m = re.search(rf"上限\s*[=:]?\s*({_NUM})", s)
    if m:
        upper = float(m.group(1))
    m = re.search(rf"下限\s*[=:]?\s*({_NUM})", s)
    if m:
        lower = float(m.group(1))
    # 阻力位 / 支撑位 (synonyms)
    m = re.search(rf"阻力(?:位)?\s*[=:]?\s*({_NUM})", s)
    if m and upper is None:
        upper = float(m.group(1))
    m = re.search(rf"支撑(?:位)?\s*[=:]?\s*({_NUM})", s)
    if m and lower is None:
        lower = float(m.group(1))

    return upper, lower


def _extract_name(s: str) -> str | None:
    m = re.search(r"name\s*=\s*([^\s]+)", s, re.I)
    if m:
        return m.group(1)
    m = re.search(r"名字?\s*[=:]\s*([^\s]+)", s)
    if m:
        return m.group(1)
    return None


_ADD_PREFIXES = re.compile(
    r"^(?:/add|add|添加|增加|加|监控|关注)\b\s*",
    re.I,
)


def _try_parse_add(s: str) -> AddCommand | None:
    m = _ADD_PREFIXES.match(s)
    if not m:
        return None
    rest = s[m.end():]
    code_m = _CODE_RE.search(rest)
    if not code_m:
        return None
    code = _normalize_code(code_m.group(1))
    upper, lower = _extract_thresholds(rest)
    # If no keyword form found, try positional `<CODE> <upper> <lower>`.
    if upper is None and lower is None:
        nums = re.findall(rf"({_NUM})", rest)
        nums = [float(n) for n in nums]
        if len(nums) == 2:
            upper, lower = nums[0], nums[1]
        elif len(nums) == 1:
            upper = nums[0]
    return AddCommand(code=code, upper=upper, lower=lower, name=_extract_name(rest))


_REMOVE_PREFIXES = re.compile(
    r"^(?:/(?:remove|rm|del|delete)|remove|rm|del|delete|删除|删|取消|停止|不监控|去掉)\b\s*",
    re.I,
)


def _try_parse_remove(s: str) -> RemoveCommand | None:
    m = _REMOVE_PREFIXES.match(s)
    if not m:
        return None
    rest = s[m.end():]
    code_m = _CODE_RE.search(rest)
    if not code_m:
        return None
    return RemoveCommand(code=_normalize_code(code_m.group(1)))


_THRESH_PREFIXES = re.compile(
    r"^(?:/(?:threshold|th)|threshold|阈值|设置|改|修改|更新)\b\s*",
    re.I,
)


def _try_parse_threshold(s: str) -> ThresholdCommand | None:
    m = _THRESH_PREFIXES.match(s)
    if not m:
        return None
    rest = s[m.end():]
    code_m = _CODE_RE.search(rest)
    if not code_m:
        return None
    upper, lower = _extract_thresholds(rest)
    if upper is None and lower is None:
        return None
    return ThresholdCommand(
        code=_normalize_code(code_m.group(1)), upper=upper, lower=lower
    )


_CHART_PREFIXES = re.compile(r"^(?:/chart|chart|图)\b\s*", re.I)


def _try_parse_chart(s: str) -> ChartCommand | None:
    m = _CHART_PREFIXES.match(s)
    if not m:
        return None
    rest = s[m.end():].strip()
    if not rest:
        return None
    parts = rest.split()
    code_m = _CODE_RE.search(parts[0])
    if not code_m:
        return None
    code = _normalize_code(code_m.group(1))
    freq = "60m"
    if len(parts) >= 2:
        candidate = parts[1].strip()
        if candidate not in ALLOWED_CHART_FREQS:
            return None
        freq = candidate
    return ChartCommand(code=code, freq=freq)
