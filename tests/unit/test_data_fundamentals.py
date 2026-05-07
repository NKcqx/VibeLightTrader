"""Unit tests for `vibe_trader.data.fundamentals`.

Covers:
  * raw-JSON → Fundamentals parsing (consensus / news / rating changes /
    earnings calendar / rating history)
  * FixtureFundamentalsClient lookup (hits, misses, malformed file)
  * NullFundamentalsClient + factory selection
  * render_for_prompt: structure, news/changes caps, earnings-blackout
    warning thresholds (well-out, in-window, post-earnings)

The committed fixture (``src/vibe_trader/data/fixtures/fundamentals/raw/``)
is the source of truth for the integration-style assertions; everything
else uses small in-test JSON dicts so failures point at the parser.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from vibe_trader.data.fundamentals import (
    AnalystConsensus,
    FixtureFundamentalsClient,
    Fundamentals,
    NullFundamentalsClient,
    build_fundamentals_client,
    parse_raw_fundamentals,
    render_for_prompt,
)

FIXTURE_DIR = (
    Path(__file__).resolve().parents[2]
    / "src/vibe_trader/data/fixtures/fundamentals/raw"
)


# ---------------------------------------------------------------------------
# Schema-level helpers.
# ---------------------------------------------------------------------------


def test_consensus_upside_pct_basic() -> None:
    c = AnalystConsensus(current_price=100.0, target_mean=125.0)
    assert c.upside_pct == pytest.approx(25.0)


def test_consensus_upside_pct_handles_missing() -> None:
    assert AnalystConsensus().upside_pct is None
    assert AnalystConsensus(current_price=100.0).upside_pct is None
    assert AnalystConsensus(target_mean=200.0).upside_pct is None


# ---------------------------------------------------------------------------
# parse_raw_fundamentals — synthetic raw payload.
# ---------------------------------------------------------------------------


def _synthetic_raw() -> dict:
    return {
        "ticker": "TEST",
        "code": "US.TEST",
        "fetched_at": "2026-05-07T14:00:00+00:00",
        "info": {
            "currentPrice": 100.0,
            "targetMeanPrice": 130.0,
            "targetHighPrice": 200.0,
            "targetLowPrice": 80.0,
            "targetMedianPrice": 125.0,
            "recommendationKey": "buy",
            "recommendationMean": 1.9,
            "numberOfAnalystOpinions": 30,
        },
        "recommendations": [
            {"index": 0, "period": "0m", "strongBuy": 5, "buy": 20, "hold": 4,
             "sell": 1, "strongSell": 0},
            {"index": 1, "period": "-1m", "strongBuy": 5, "buy": 19, "hold": 5,
             "sell": 1, "strongSell": 0},
        ],
        "upgrades_downgrades": [
            {
                "GradeDate": "2026-05-01T10:00:00",
                "Firm": "Demo Bank",
                "ToGrade": "Buy",
                "FromGrade": "Hold",
                "Action": "up",
                "priceTargetAction": "Raises",
                "currentPriceTarget": 150.0,
                "priorPriceTarget": 130.0,
            },
            {
                "GradeDate": "2026-04-15T15:30:00",
                "Firm": "OldBank",
                "ToGrade": "Buy",
                "FromGrade": "Buy",
                "Action": "main",
                "priceTargetAction": "Maintains",
                "currentPriceTarget": 140.0,
                "priorPriceTarget": 140.0,
            },
        ],
        "news": [
            {
                "id": "n1",
                "content": {
                    "title": "Demo Co beats estimates",
                    "summary": "Strong quarter on AI tailwinds.",
                    "pubDate": "2026-05-06T18:00:00Z",
                    "provider": {"displayName": "Yahoo Finance"},
                    "canonicalUrl": {"url": "https://example.com/n1"},
                },
            },
            {
                "id": "n2",
                "content": {
                    "title": "Mid-quarter analyst note",
                    "description": "Reiterates Buy.",
                    "displayTime": "2026-05-04T12:00:00Z",
                    "provider": {"displayName": "Reuters"},
                    "clickThroughUrl": {"url": "https://example.com/n2"},
                },
            },
        ],
        "calendar": {
            "Earnings Date": ["2026-05-21"],
            "Earnings High": 1.99,
            "Earnings Low": 1.50,
            "Earnings Average": 1.75,
            "Revenue High": 80_000_000_000,
            "Revenue Low": 75_000_000_000,
            "Revenue Average": 77_000_000_000,
            "Dividend Date": "2026-04-01",
            "Ex-Dividend Date": "2026-03-11",
        },
    }


def test_parse_raw_fundamentals_basic_shape() -> None:
    fund = parse_raw_fundamentals(_synthetic_raw())
    assert fund.code == "US.TEST"
    assert fund.fetched_at.tzinfo is not None
    # consensus
    assert fund.consensus.recommendation_key == "buy"
    assert fund.consensus.target_mean == 130.0
    assert fund.consensus.num_analysts == 30
    assert fund.consensus.rating_distribution["buy"] == 20
    assert fund.consensus.upside_pct == pytest.approx(30.0)
    # rating changes
    assert len(fund.recent_rating_changes) == 2
    rc0 = fund.recent_rating_changes[0]
    assert rc0.firm == "Demo Bank"
    assert rc0.to_grade == "Buy"
    assert rc0.from_grade == "Hold"
    assert rc0.current_price_target == 150.0
    # news
    assert len(fund.news) == 2
    assert fund.news[0].title == "Demo Co beats estimates"
    assert fund.news[0].publisher == "Yahoo Finance"
    assert fund.news[0].url == "https://example.com/n1"
    assert fund.news[1].url == "https://example.com/n2"  # clickThroughUrl fallback
    # earnings
    assert fund.earnings.earnings_date == date(2026, 5, 21)
    assert fund.earnings.earnings_avg == 1.75
    assert fund.earnings.dividend_date == date(2026, 4, 1)
    # rating history
    assert len(fund.rating_history) == 2
    assert fund.rating_history[0]["period"] == "0m"


def test_parse_raw_fundamentals_drops_news_without_title() -> None:
    raw = _synthetic_raw()
    raw["news"].append({"id": "broken", "content": {"summary": "no title"}})
    raw["news"].append({"id": "broken2", "content": None})
    fund = parse_raw_fundamentals(raw, max_news=10)
    titles = [n.title for n in fund.news]
    assert "Demo Co beats estimates" in titles
    assert all("no title" not in t for t in titles)


def test_parse_raw_fundamentals_caps_rating_changes() -> None:
    raw = _synthetic_raw()
    raw["upgrades_downgrades"] = raw["upgrades_downgrades"] * 30  # 60 rows
    fund = parse_raw_fundamentals(raw, max_rating_changes=5)
    assert len(fund.recent_rating_changes) == 5


def test_parse_raw_fundamentals_handles_missing_blocks() -> None:
    fund = parse_raw_fundamentals(
        {"code": "US.X", "fetched_at": "2026-05-01T00:00:00+00:00"}
    )
    assert fund.code == "US.X"
    assert fund.consensus.recommendation_key is None
    assert fund.recent_rating_changes == []
    assert fund.news == []
    assert fund.earnings.earnings_date is None


# ---------------------------------------------------------------------------
# Real fixture — guards against regressions in the parser when paired with
# the committed snapshot.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not (FIXTURE_DIR / "US.NVDA.json").exists(),
    reason="NVDA fixture not committed",
)
def test_real_fixture_nvda_parses() -> None:
    raw = json.loads((FIXTURE_DIR / "US.NVDA.json").read_text())
    fund = parse_raw_fundamentals(raw)
    assert fund.code == "US.NVDA"
    assert fund.consensus.num_analysts and fund.consensus.num_analysts > 20
    assert fund.consensus.recommendation_key  # populated
    assert fund.recent_rating_changes  # at least one
    assert fund.news  # at least one
    assert fund.earnings.earnings_date is not None


# ---------------------------------------------------------------------------
# FixtureFundamentalsClient.
# ---------------------------------------------------------------------------


def test_fixture_client_finds_by_full_code(tmp_path: Path) -> None:
    (tmp_path / "US.TEST.json").write_text(json.dumps(_synthetic_raw()))
    client = FixtureFundamentalsClient(root=tmp_path)
    fund = client.fetch("US.TEST")
    assert isinstance(fund, Fundamentals)
    assert fund.code == "US.TEST"


def test_fixture_client_falls_back_to_short_name(tmp_path: Path) -> None:
    # Note: file lives at TEST.json (no US. prefix).
    (tmp_path / "TEST.json").write_text(json.dumps(_synthetic_raw()))
    client = FixtureFundamentalsClient(root=tmp_path)
    fund = client.fetch("US.TEST")
    assert fund is not None
    assert fund.consensus.recommendation_key == "buy"


def test_fixture_client_returns_none_when_missing(tmp_path: Path) -> None:
    client = FixtureFundamentalsClient(root=tmp_path)
    assert client.fetch("US.UNKNOWN") is None


def test_fixture_client_returns_none_for_malformed_json(tmp_path: Path) -> None:
    (tmp_path / "US.X.json").write_text("{not json")
    client = FixtureFundamentalsClient(root=tmp_path)
    assert client.fetch("US.X") is None


def test_null_client_always_none() -> None:
    client = NullFundamentalsClient()
    assert client.fetch("US.NVDA") is None


def test_build_fundamentals_client_factory(tmp_path: Path) -> None:
    assert isinstance(build_fundamentals_client("none"), NullFundamentalsClient)
    fc = build_fundamentals_client("fixture", fixture_dir=str(tmp_path))
    assert isinstance(fc, FixtureFundamentalsClient)
    with pytest.raises(ValueError, match="yfinance"):
        build_fundamentals_client("yfinance")
    with pytest.raises(ValueError, match="unknown"):
        build_fundamentals_client("nope")


# ---------------------------------------------------------------------------
# render_for_prompt.
# ---------------------------------------------------------------------------


def _fund_for_render() -> Fundamentals:
    return parse_raw_fundamentals(_synthetic_raw())


def test_render_for_prompt_none_yields_empty() -> None:
    assert render_for_prompt(None) == ""


def test_render_for_prompt_basic_blocks_present() -> None:
    out = render_for_prompt(_fund_for_render())
    assert "Wall Street consensus" in out
    assert "Rating distribution" in out
    assert "Recent rating changes" in out
    assert "Recent news headlines" in out
    assert "Next earnings" in out


def test_render_for_prompt_caps_news_and_changes() -> None:
    raw = _synthetic_raw()
    raw["news"] = raw["news"] * 6  # 12 entries
    raw["upgrades_downgrades"] = raw["upgrades_downgrades"] * 6  # 12 entries
    fund = parse_raw_fundamentals(raw, max_news=12, max_rating_changes=12)
    out = render_for_prompt(fund, max_news=2, max_changes=3)
    # 2 news bullet lines starting with "- ["
    assert sum(1 for line in out.splitlines() if line.startswith("- [")) == 2
    # rating-change bullets begin with "- 2026-..." pattern
    rc_lines = [
        ln for ln in out.splitlines() if ln.startswith("- 2026-") and "PT" in ln
    ]
    assert len(rc_lines) == 3


def test_render_for_prompt_blackout_warning_in_window() -> None:
    fund = _fund_for_render()  # earnings 2026-05-21
    out = render_for_prompt(fund, today=date(2026, 5, 19), blackout_days=3)
    assert "WARNING" in out
    assert "in 2 day" in out
    assert "Decline new BUY" in out


def test_render_for_prompt_no_blackout_outside_window() -> None:
    fund = _fund_for_render()
    out = render_for_prompt(fund, today=date(2026, 5, 7), blackout_days=3)
    assert "WARNING" not in out
    assert "in 14 day" in out


def test_render_for_prompt_post_earnings() -> None:
    fund = _fund_for_render()
    out = render_for_prompt(fund, today=date(2026, 5, 25), blackout_days=3)
    assert "WARNING" not in out
    assert "already reported" in out


def test_render_for_prompt_no_today_skips_warning() -> None:
    fund = _fund_for_render()
    out = render_for_prompt(fund, today=None)
    assert "WARNING" not in out
    assert "in " not in out.split("Next earnings:")[1].splitlines()[0]
