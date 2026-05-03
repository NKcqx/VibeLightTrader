from equity_monitor.events.grammar import ChartCommand, parse


def test_chart_default_freq() -> None:
    cmd = parse("/chart US.AAPL")
    assert isinstance(cmd, ChartCommand)
    assert cmd.code == "US.AAPL"
    assert cmd.freq == "60m"


def test_chart_explicit_freq() -> None:
    cmd = parse("/chart US.AAPL D")
    assert isinstance(cmd, ChartCommand)
    assert cmd.code == "US.AAPL"
    assert cmd.freq == "D"


def test_chart_lowercase_short_freq() -> None:
    for f in ("5m", "15m", "30m", "60m"):
        cmd = parse(f"/chart AAPL {f}")
        assert isinstance(cmd, ChartCommand)
        assert cmd.code == "US.AAPL"
        assert cmd.freq == f


def test_chart_weekly_freq() -> None:
    cmd = parse("/chart US.AAPL W")
    assert isinstance(cmd, ChartCommand)
    assert cmd.freq == "W"


def test_chart_unknown_freq_returns_none() -> None:
    """Unknown freq strings reject the whole command."""
    assert parse("/chart AAPL bogus") is None
    assert parse("/chart AAPL 1m") is None  # 1m intentionally not allowed for /chart


def test_chart_chinese_alias_returns_default_60m() -> None:
    cmd = parse("图 US.AAPL")
    assert isinstance(cmd, ChartCommand)
    assert cmd.code == "US.AAPL"
    assert cmd.freq == "60m"


def test_chart_no_code_returns_none() -> None:
    assert parse("/chart") is None
    assert parse("/chart   ") is None


def test_non_chart_text_doesnt_become_chart_command() -> None:
    assert not isinstance(parse("/list"), ChartCommand)
    assert not isinstance(parse("帮助"), ChartCommand)
