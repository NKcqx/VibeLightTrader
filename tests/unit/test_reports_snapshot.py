from datetime import datetime, timezone

import pandas as pd

from equity_monitor.reports.snapshot import (
    SnapshotRequest,
    TradeMarker,
    render_snapshot,
)


def _toy_df() -> pd.DataFrame:
    idx = pd.date_range("2026-04-01", periods=10, freq="D", tz="UTC")
    return pd.DataFrame(
        {
            "open":   [100, 101, 102, 99,  98,  100, 102, 104, 103, 105],
            "high":   [102, 103, 103, 100, 99,  101, 105, 106, 105, 107],
            "low":    [99,  100, 101, 97,  96,  99,  101, 103, 102, 104],
            "close":  [101, 102, 99,  98,  100, 102, 104, 105, 104, 106],
            "volume": [1_000] * 10,
        },
        index=idx,
    )


def test_render_snapshot_writes_png_and_returns_path(tmp_path) -> None:
    req = SnapshotRequest(
        code="US.AAPL",
        freq="D",
        df=_toy_df(),
        markers=[
            TradeMarker(
                ts=datetime(2026, 4, 4, tzinfo=timezone.utc),
                side="buy", qty=100, price=98.0,
            ),
            TradeMarker(
                ts=datetime(2026, 4, 9, tzinfo=timezone.utc),
                side="sell", qty=100, price=104.0,
            ),
        ],
        avg_cost=98.0,
        current_price=106.0,
        out_dir=tmp_path,
    )
    out_path = render_snapshot(req)
    assert out_path.exists()
    assert out_path.suffix == ".png"
    assert out_path.stat().st_size > 1024  # non-trivial bytes


def test_render_snapshot_without_markers_or_position(tmp_path) -> None:
    req = SnapshotRequest(
        code="US.TSLA",
        freq="60m",
        df=_toy_df(),
        markers=[],
        avg_cost=None,
        current_price=None,
        out_dir=tmp_path,
    )
    out_path = render_snapshot(req)
    assert out_path.exists()


def test_render_snapshot_empty_df_returns_placeholder(tmp_path) -> None:
    req = SnapshotRequest(
        code="US.AAPL",
        freq="D",
        df=pd.DataFrame(columns=["open", "high", "low", "close", "volume"]),
        markers=[],
        avg_cost=None,
        current_price=None,
        out_dir=tmp_path,
    )
    out_path = render_snapshot(req)
    assert out_path.exists()  # placeholder PNG with "no data" message
    assert out_path.stat().st_size > 200  # actual PNG, not zero-byte
    assert out_path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"  # PNG magic


def test_render_snapshot_with_malformed_df_falls_back(tmp_path) -> None:
    df = pd.DataFrame(
        {
            "open": [100.0],
            "high": [101.0],
            "low": [99.0],
            "close": [100.5],
            "volume": [100],
        },
        index=[0],
    )
    req = SnapshotRequest(
        code="US.BAD",
        freq="D",
        df=df,
        markers=[],
        avg_cost=None,
        current_price=None,
        out_dir=tmp_path,
    )
    out_path = render_snapshot(req)
    assert out_path.exists()
    assert out_path.stat().st_size > 200
