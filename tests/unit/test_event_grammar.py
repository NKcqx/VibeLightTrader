from __future__ import annotations

import pytest

from equity_monitor.events.grammar import (
    AddCommand,
    HelpCommand,
    ListCommand,
    RemoveCommand,
    ThresholdCommand,
    parse,
)


# ---- LIST ----------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "/list",
        "list",
        "看监控",
        "看列表",
        "监控列表",
        "列表",
        "ls",
        "  /list  ",
    ],
)
def test_list_variants(text: str) -> None:
    assert isinstance(parse(text), ListCommand)


# ---- HELP ----------------------------------------------------------------


@pytest.mark.parametrize("text", ["/help", "help", "帮助", "?", "？"])
def test_help_variants(text: str) -> None:
    assert isinstance(parse(text), HelpCommand)


# ---- ADD -----------------------------------------------------------------


def test_add_slash_kwargs() -> None:
    out = parse("/add US.AAPL upper=200 lower=165")
    assert out == AddCommand(code="US.AAPL", upper=200.0, lower=165.0, name=None)


def test_add_zh_kwargs() -> None:
    out = parse("添加 US.AAPL 上限200 下限165")
    assert out == AddCommand(code="US.AAPL", upper=200.0, lower=165.0, name=None)


def test_add_zh_with_spaces_and_punctuation() -> None:
    out = parse("添加 US.AAPL  上限 = 200  下限 = 165")
    assert out == AddCommand(code="US.AAPL", upper=200.0, lower=165.0, name=None)


def test_add_short_code_normalized_to_us_market() -> None:
    """Bare ticker → US.<ticker>"""
    out = parse("/add aapl upper=200 lower=165")
    assert out == AddCommand(code="US.AAPL", upper=200.0, lower=165.0, name=None)


def test_add_synonyms() -> None:
    """关注 / 监控 / 增加 are all add."""
    for prefix in ("关注", "监控", "增加", "加"):
        out = parse(f"{prefix} US.AAPL 上限200 下限165")
        assert isinstance(out, AddCommand) and out.upper == 200.0


def test_add_positional_two_numbers() -> None:
    out = parse("/add US.AAPL 200 165")
    assert out == AddCommand(code="US.AAPL", upper=200.0, lower=165.0, name=None)


def test_add_no_thresholds() -> None:
    out = parse("监控 US.TSLA")
    assert out == AddCommand(code="US.TSLA", upper=None, lower=None, name=None)


def test_add_with_name() -> None:
    out = parse("/add US.AAPL upper=200 lower=165 name=Apple")
    assert out == AddCommand(code="US.AAPL", upper=200.0, lower=165.0, name="Apple")


def test_add_resistance_support_aliases() -> None:
    """阻力位 / 支撑位 same as 上限 / 下限."""
    out = parse("添加 US.AAPL 阻力位 200 支撑位 165")
    assert isinstance(out, AddCommand)
    assert out.upper == 200.0
    assert out.lower == 165.0


# ---- REMOVE --------------------------------------------------------------


def test_remove_slash() -> None:
    assert parse("/remove US.AAPL") == RemoveCommand(code="US.AAPL")


@pytest.mark.parametrize(
    "phrase",
    [
        "删除 US.AAPL",
        "删 US.AAPL",
        "取消 US.AAPL",
        "停止 US.AAPL",
        "不监控 US.AAPL",
        "rm US.AAPL",
    ],
)
def test_remove_synonyms(phrase: str) -> None:
    assert parse(phrase) == RemoveCommand(code="US.AAPL")


def test_remove_short_code_normalized() -> None:
    assert parse("删除 AAPL") == RemoveCommand(code="US.AAPL")


# ---- THRESHOLD -----------------------------------------------------------


def test_threshold_update_upper_only() -> None:
    out = parse("阈值 US.AAPL 上限205")
    assert out == ThresholdCommand(code="US.AAPL", upper=205.0, lower=None)


def test_threshold_update_both() -> None:
    out = parse("/threshold US.AAPL upper=210 lower=170")
    assert out == ThresholdCommand(code="US.AAPL", upper=210.0, lower=170.0)


def test_threshold_synonyms() -> None:
    assert isinstance(parse("修改 US.AAPL 上限205"), ThresholdCommand)
    assert isinstance(parse("更新 US.AAPL 上限205"), ThresholdCommand)


def test_threshold_without_value_returns_none() -> None:
    """Threshold prefix but no number → not a valid threshold command."""
    assert parse("阈值 US.AAPL") is None


# ---- IGNORE / NONE -------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "",
        "hi there",
        "你好",
        "thanks bot",
        "what's the price?",
        # Sentences mentioning a code but no command verb
        "AAPL is doing great today",
    ],
)
def test_unrecognized_returns_none(text: str) -> None:
    assert parse(text) is None


def test_strips_at_mention() -> None:
    out = parse("@小助手 /list")
    assert isinstance(out, ListCommand)
    out = parse("@bot 添加 US.AAPL 上限200 下限165")
    assert isinstance(out, AddCommand) and out.upper == 200.0
