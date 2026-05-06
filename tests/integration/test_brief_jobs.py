from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from sqlalchemy.orm import sessionmaker

from vibe_trader.config import AppConfig, WatchlistConfig
from vibe_trader.db import session_scope
from vibe_trader.futu_client import FakeFutuClient, Snapshot
from vibe_trader.models import Position, Symbol, Trade
from vibe_trader.scheduler.jobs import run_closing_brief, run_morning_brief


def _snap(code: str, *, last: float, open_: float) -> Snapshot:
    return Snapshot(
        code=code,
        last_price=last,
        open_price=open_,
        high_price=max(last, open_) + 1.0,
        low_price=min(last, open_) - 1.0,
        volume=12_000_000,
        turnover=2.184e9,
        update_time=datetime(2026, 5, 4, 14, 30),
    )


@pytest.mark.integration
def test_morning_brief_pushes_card(
    factory: sessionmaker,
    fake_futu: FakeFutuClient,
    app_cfg: AppConfig,
    watchlist: WatchlistConfig,
) -> None:
    with session_scope(factory) as s:
        s.add(
            Symbol(
                code="US.AAPL",
                name="Apple",
                upper_threshold=200.0,
                lower_threshold=165.0,
            )
        )

    fake_futu.set_snapshot(_snap("US.AAPL", last=185.0, open_=180.0))

    sent: list[dict[str, Any]] = []

    def fake_sender(card, open_id, receiver_type):  # type: ignore[no-untyped-def]
        sent.append(card)
        return "om_test"

    out = run_morning_brief(
        client=fake_futu,
        factory=factory,
        cfg=app_cfg,
        watchlist=watchlist,
        send_card_fn=fake_sender,
    )
    assert out["rows"] == 1
    assert out["pushed"] == 1
    assert "US.AAPL" in str(sent[0])
    title = sent[0]["header"]["title"]["content"]
    assert "开盘后1h盘点" in title


@pytest.mark.integration
def test_closing_brief_uses_correct_label(
    factory: sessionmaker,
    fake_futu: FakeFutuClient,
    app_cfg: AppConfig,
    watchlist: WatchlistConfig,
) -> None:
    with session_scope(factory) as s:
        s.add(Symbol(code="US.AAPL", name="Apple"))
    fake_futu.set_snapshot(_snap("US.AAPL", last=178.0, open_=180.0))

    sent: list[dict[str, Any]] = []

    def sender(card, *_):  # type: ignore[no-untyped-def]
        sent.append(card)
        return "om_close"

    out = run_closing_brief(
        client=fake_futu,
        factory=factory,
        cfg=app_cfg,
        watchlist=watchlist,
        send_card_fn=sender,
    )
    assert out["pushed"] == 1
    assert "收盘盘点" in sent[0]["header"]["title"]["content"]


@pytest.mark.integration
def test_brief_no_snapshot_no_rows_no_crash(
    factory: sessionmaker,
    fake_futu: FakeFutuClient,
    app_cfg: AppConfig,
    watchlist: WatchlistConfig,
) -> None:
    """If snapshot returns nothing, brief still pushes an empty (rows=0) card."""
    out = run_morning_brief(
        client=fake_futu,
        factory=factory,
        cfg=app_cfg,
        watchlist=watchlist,
        send_card_fn=lambda *args: "om_empty",
    )
    assert out["rows"] == 0
    assert out["pushed"] == 1


@pytest.mark.integration
def test_brief_summary_includes_top_gainers_losers(
    factory: sessionmaker,
    fake_futu: FakeFutuClient,
    app_cfg: AppConfig,
) -> None:
    """With 3+ symbols, the summary must list top gainers and losers."""
    from vibe_trader.config import SymbolConfig

    multi = WatchlistConfig(
        symbols=[
            SymbolConfig(code="US.AAPL", name="Apple"),
            SymbolConfig(code="US.NVDA", name="NVIDIA"),
            SymbolConfig(code="US.TSLA", name="Tesla"),
        ]
    )
    with session_scope(factory) as s:
        for code, name in [
            ("US.AAPL", "Apple"),
            ("US.NVDA", "NVIDIA"),
            ("US.TSLA", "Tesla"),
        ]:
            s.add(Symbol(code=code, name=name))

    fake_futu.set_snapshot(_snap("US.AAPL", last=185.0, open_=180.0))
    fake_futu.set_snapshot(_snap("US.NVDA", last=140.0, open_=130.0))
    fake_futu.set_snapshot(_snap("US.TSLA", last=170.0, open_=180.0))

    sent: list[dict[str, Any]] = []

    def sender(card, *_):  # type: ignore[no-untyped-def]
        sent.append(card)
        return "om_top"

    out = run_morning_brief(
        client=fake_futu,
        factory=factory,
        cfg=app_cfg,
        watchlist=multi,
        send_card_fn=sender,
        now_utc=datetime(2026, 5, 4, 14, 0, tzinfo=timezone.utc),
    )
    assert out["rows"] == 3
    body = str(sent[0])
    assert "Top 涨" in body
    assert "Top 跌" in body
    assert "US.NVDA" in body


@pytest.mark.integration
def test_brief_appends_paper_pnl_when_position_exists(
    factory: sessionmaker,
    fake_futu: FakeFutuClient,
    app_cfg: AppConfig,
    watchlist: WatchlistConfig,
) -> None:
    """When a Position row exists, brief card includes '纸面盘 P&L' section."""
    with session_scope(factory) as s:
        sym = Symbol(
            code="US.AAPL", name="Apple", upper_threshold=200.0, lower_threshold=165.0
        )
        s.add(sym)
        s.flush()
        s.add(Position(symbol_id=sym.id, qty=10, avg_cost=180.0))
        s.add(
            Trade(
                symbol_id=sym.id,
                ts=datetime(2026, 5, 4, 13, 30, tzinfo=timezone.utc),
                side="BUY",
                qty=10,
                price=180.0,
                status="FILLED",
                futu_order_id="po_test",
            )
        )

    fake_futu.set_snapshot(_snap("US.AAPL", last=185.0, open_=180.0))

    sent: list[dict[str, Any]] = []

    def sender(card, *_):  # type: ignore[no-untyped-def]
        sent.append(card)
        return "om_pnl"

    out = run_closing_brief(
        client=fake_futu,
        factory=factory,
        cfg=app_cfg,
        watchlist=watchlist,
        send_card_fn=sender,
        now_utc=datetime(2026, 5, 4, 21, 0, tzinfo=timezone.utc),
    )
    assert out["pushed"] == 1
    body = str(sent[0])
    assert "纸面盘 P&L" in body or "P&L" in body
    assert "US.AAPL" in body
    # mark = 185, avg_cost = 180, qty=10 → unrealized = +50
    assert "+50" in body or "浮盈" in body


@pytest.mark.integration
def test_brief_omits_pnl_section_when_no_positions(
    factory: sessionmaker,
    fake_futu: FakeFutuClient,
    app_cfg: AppConfig,
    watchlist: WatchlistConfig,
) -> None:
    """No positions and no trades → no P&L section in card."""
    with session_scope(factory) as s:
        s.add(Symbol(code="US.AAPL", name="Apple"))

    fake_futu.set_snapshot(_snap("US.AAPL", last=185.0, open_=180.0))

    sent: list[dict[str, Any]] = []

    def sender(card, *_):  # type: ignore[no-untyped-def]
        sent.append(card)
        return "om_x"

    run_closing_brief(
        client=fake_futu,
        factory=factory,
        cfg=app_cfg,
        watchlist=watchlist,
        send_card_fn=sender,
        now_utc=datetime(2026, 5, 4, 21, 0, tzinfo=timezone.utc),
    )
    body = str(sent[0])
    assert "纸面盘" not in body  # P&L section absent
