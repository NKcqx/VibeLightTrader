# Phase 3 (Scoped): K-Line Snapshot Visualization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add static K-line PNG snapshots that show OHLCV + my paper-trade BUY/SELL markers + my average cost line, delivered to Lark both automatically (every signal alert) and on-demand (`/chart US.AAPL D`). Multiple K-line frequencies (5m / 15m / 60m / D / W) supported via the existing `OpenDClient.kline` interface.

**Architecture:**
1. `mplfinance` produces a single PNG per snapshot. All K-line data is fetched on-demand from OpenD; no DB caching, no schema changes.
2. `lark-cli im +messages-send --type image` uploads + sends. The image is a separate message that follows the existing alert card.
3. A new `/chart <code> [freq]` listener command parses, fetches, renders, and sends an on-demand snapshot.

**Tech Stack:**
- New: `mplfinance>=0.12.10` (MIT, matplotlib-backed K-line rendering)
- Existing: Python 3.11, `futu-api`, `lark-cli`, `click`, `pytest`, `freezegun`, `jinja2`, `tenacity`

**Scope explicitly OUT (deferred):**
- Strategy abstraction layer (Protocol / Context / Decision / registry)
- Per-strategy P&L / equity / max-drawdown
- `/positions`, `/pnl`, `/history` listener commands
- QuantStats tearsheet
- `BackfillState` cursor for incremental K-line pulls
- `strategy_name` columns on `Signal` / `Trade`

These can land later as separate plans without retrofitting anything in this one.

---

## Pre-flight

Commit pending listener-default changes before starting Task 1. Run from the repo root:

```bash
cd /Users/bytedance/Documents/Code/equity-monitor
git status
# Expect: modified src/equity_monitor/cli/main.py + src/equity_monitor/events/listener.py
git add -A && git commit -m "fix(listener): switch default backend to websocket after lark-cli upgrade"
```

If `git status` shows other modified files, **stop and ask the user** before committing.

---

## File Structure

**Create:**
- `src/equity_monitor/reports/snapshot.py` — mplfinance PNG renderer
- `src/equity_monitor/reports/lark_image.py` — `send_image(path, open_id, receiver_type)` via lark-cli
- `tests/unit/test_reports_snapshot.py`
- `tests/unit/test_reports_lark_image.py`
- `tests/unit/test_event_grammar_chart.py`
- `tests/integration/test_intraday_alert_with_snapshot.py`
- `tests/integration/test_listener_chart.py`
- `scripts/smoke_phase3.py`
- `var/snapshots/.gitkeep` (output directory)

**Modify:**
- `pyproject.toml` — add `mplfinance>=0.12.10` to `dependencies`
- `src/equity_monitor/futu_client.py` — widen `kline()` ktype map to support `K_5M / K_15M / K_30M / K_60M / K_DAY / K_WEEK`; add `FREQ_TO_KTYPE` constant
- `src/equity_monitor/data/kline.py` — add `fetch_klines_multi()` helper (thin loop over freqs)
- `src/equity_monitor/scheduler/jobs.py` — `run_intraday_check()` accepts an optional `send_image_fn` and `snapshot_dir`; `_push_for_code()` renders + sends a snapshot after the card
- `src/equity_monitor/scheduler/runner.py` — pass the default image sender into `run_intraday_check`
- `src/equity_monitor/cli/main.py` — add `chart` subcommand for ad-hoc CLI use
- `src/equity_monitor/events/grammar.py` — parse `/chart <code> [freq]`
- `src/equity_monitor/events/apply.py` — handle `ChartCommand`: fetch K-line, render snapshot, return image path
- `src/equity_monitor/events/listener.py` — when an apply returns a `ChartReplyPayload`, send the PNG via `send_image` after the text/card reply
- `README.md` — Phase 3 section
- `docs/lark_app_description.md` — add `/chart` to command reference
- `.gitignore` — add `var/snapshots/`

---

## Task 1: mplfinance snapshot renderer

**Files:**
- Modify: `pyproject.toml`
- Create: `src/equity_monitor/reports/snapshot.py`
- Test: `tests/unit/test_reports_snapshot.py`

- [ ] **Step 1: Add dependency**

Edit `pyproject.toml`, in `[project] dependencies`, add a new line:

```toml
"mplfinance>=0.12.10",
```

Install:

```bash
cd /Users/bytedance/Documents/Code/equity-monitor
pip install -e '.[dev]'
```

- [ ] **Step 2: Write failing test**

Create `tests/unit/test_reports_snapshot.py`:

```python
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
```

- [ ] **Step 3: Run test, expect FAIL**

```bash
pytest tests/unit/test_reports_snapshot.py -v
```

Expected: `ModuleNotFoundError: No module named 'equity_monitor.reports.snapshot'`.

- [ ] **Step 4: Implement `reports/snapshot.py`**

Create `src/equity_monitor/reports/snapshot.py`:

```python
"""Render OHLCV + paper-trade markers as a static PNG (Phase 3, scoped).

Uses mplfinance with the 'charles' style:
  - Green ▲ markers for BUY fills
  - Red ▼ markers for SELL fills
  - Orange dashed horizontal line at average cost
  - Steel-blue dashed horizontal line at current price

The result is a single self-contained PNG that's fine to ship through
the Lark image API. No interactive features; users view it in the Lark
app on phone or desktop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

import matplotlib.pyplot as plt
import mplfinance as mpf
import pandas as pd


@dataclass(frozen=True)
class TradeMarker:
    ts: datetime
    side: Literal["buy", "sell"]
    qty: int
    price: float


@dataclass(frozen=True)
class SnapshotRequest:
    code: str
    freq: str
    df: pd.DataFrame                                # OHLCV indexed by ts (UTC, ascending)
    markers: list[TradeMarker] = field(default_factory=list)
    avg_cost: float | None = None
    current_price: float | None = None
    out_dir: Path | None = None                     # default var/snapshots/


def _markers_series(
    df: pd.DataFrame, markers: list[TradeMarker], side: str
) -> pd.Series:
    """Build a DataFrame-aligned series with NaN where no marker, else price."""
    s = pd.Series(index=df.index, dtype=float)
    for m in markers:
        if m.side != side:
            continue
        # Snap to the bar at-or-before m.ts (markers don't always land exactly).
        idx = df.index.get_indexer([pd.Timestamp(m.ts)], method="ffill")
        if idx[0] == -1:
            continue
        s.iloc[idx[0]] = m.price
    return s


def _safe_filename(code: str, freq: str) -> str:
    safe = code.replace(".", "_").replace("/", "_")
    return f"{safe}_{freq}_{datetime.utcnow():%Y%m%d_%H%M%S}.png"


def render_snapshot(req: SnapshotRequest) -> Path:
    """Render `req` to a PNG under `out_dir` and return the path."""
    out_dir = req.out_dir or Path("var/snapshots")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / _safe_filename(req.code, req.freq)

    if req.df.empty:
        fig, ax = plt.subplots(figsize=(8, 4.5), dpi=110)
        ax.text(
            0.5, 0.5,
            f"{req.code} ({req.freq}) — 暂无 K 线数据",
            ha="center", va="center", fontsize=14,
        )
        ax.axis("off")
        fig.savefig(out_path, bbox_inches="tight")
        plt.close(fig)
        return out_path

    addplots: list = []
    buy_s = _markers_series(req.df, req.markers, "buy")
    sell_s = _markers_series(req.df, req.markers, "sell")
    if buy_s.notna().any():
        addplots.append(
            mpf.make_addplot(
                buy_s, type="scatter", marker="^",
                markersize=140, color="#2ecc71", panel=0,
            )
        )
    if sell_s.notna().any():
        addplots.append(
            mpf.make_addplot(
                sell_s, type="scatter", marker="v",
                markersize=140, color="#e74c3c", panel=0,
            )
        )

    hlines: dict[str, list] = {
        "hlines": [], "colors": [], "linestyle": "--", "linewidths": 1,
    }
    if req.avg_cost is not None:
        hlines["hlines"].append(req.avg_cost)
        hlines["colors"].append("orange")
    if req.current_price is not None:
        hlines["hlines"].append(req.current_price)
        hlines["colors"].append("steelblue")
    hlines_arg = hlines if hlines["hlines"] else None

    title = f"{req.code} · {req.freq}"
    if req.current_price is not None:
        title += f"  ${req.current_price:.2f}"
    if req.avg_cost is not None:
        title += f" (avg ${req.avg_cost:.2f})"

    mpf.plot(
        req.df,
        type="candle",
        style="charles",
        addplot=addplots,
        hlines=hlines_arg,
        volume=True,
        figsize=(9, 6),
        figratio=(16, 9),
        title=title,
        savefig=dict(fname=str(out_path), dpi=120, bbox_inches="tight"),
    )
    plt.close("all")
    return out_path
```

- [ ] **Step 5: Run test, expect PASS**

```bash
pytest tests/unit/test_reports_snapshot.py -v
```

Expected: 3 PASS.

- [ ] **Step 6: Add output dir + .gitignore**

```bash
mkdir -p var/snapshots
touch var/snapshots/.gitkeep
```

Edit `.gitignore`, append:

```
# Phase 3 snapshot output
var/snapshots/*.png
!var/snapshots/.gitkeep
```

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml src/equity_monitor/reports/snapshot.py tests/unit/test_reports_snapshot.py var/snapshots/.gitkeep .gitignore
git commit -m "feat(p3): mplfinance snapshot renderer with BUY/SELL markers + cost line"
```

---

## Task 2: lark-cli image sender wrapper

**Files:**
- Create: `src/equity_monitor/reports/lark_image.py`
- Test: `tests/unit/test_reports_lark_image.py`

`lark-cli` exposes image-message sending via `im +messages-send --type image --content @<path>` (the `@` prefix tells lark-cli to upload the file and substitute the resulting `image_key`). Same retry / error semantics as `reports/lark.py:send_card`.

- [ ] **Step 1: Confirm the lark-cli flag shape**

```bash
lark-cli im +messages-send --help 2>&1 | grep -E "type|content|image" | head -20
```

Read the output. The expected flags are `--type image` + `--content @<absolute_path>`. If the actual flag for image attachments differs (e.g. some versions want `--image <path>` instead of `--content @<path>`), update Step 4's implementation and Step 2's expected `args` accordingly. Document the actual form you confirmed in the docstring.

- [ ] **Step 2: Write failing test**

Create `tests/unit/test_reports_lark_image.py`:

```python
import subprocess

import pytest

from equity_monitor.reports.lark_image import LarkImageError, send_image


def test_send_image_invokes_lark_cli_and_returns_msg_id(tmp_path, monkeypatch) -> None:
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    captured: dict = {}

    class FakeRes:
        returncode = 0
        stdout = '{"message_id": "om_xxx"}\n'
        stderr = ""

    def fake_run(args, **kw):
        captured["args"] = args
        return FakeRes()

    monkeypatch.setattr(subprocess, "run", fake_run)
    msg_id = send_image(img, open_id="ou_abc", receiver_type="open_id")
    assert msg_id == "om_xxx"
    assert "im" in captured["args"]
    cmdline = " ".join(captured["args"])
    assert "image" in cmdline
    assert str(img.absolute()) in cmdline


def test_send_image_raises_on_nonzero_rc(tmp_path, monkeypatch) -> None:
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG")

    class BadRes:
        returncode = 7
        stdout = ""
        stderr = "boom\n"

    def fake_run(args, **kw):
        return BadRes()

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(LarkImageError, match="boom"):
        send_image(img, open_id="ou_abc", receiver_type="open_id")


def test_send_image_raises_on_missing_file() -> None:
    from pathlib import Path
    with pytest.raises(LarkImageError, match="file not found"):
        send_image(Path("/tmp/nonexistent_xyz.png"),
                   open_id="ou", receiver_type="open_id")
```

- [ ] **Step 3: Run test, expect FAIL**

```bash
pytest tests/unit/test_reports_lark_image.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 4: Implement `reports/lark_image.py`**

Create `src/equity_monitor/reports/lark_image.py`:

```python
"""Send a PNG/JPG to Lark via lark-cli (Phase 3 image messages).

Mirrors the retry / error contract of `reports/lark.py:send_card`. The
underlying `lark-cli im +messages-send --type image --content @<path>`
command both uploads the file and sends it as a single image message;
no separate upload/key dance is required at the caller.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Literal

from tenacity import retry, stop_after_attempt, wait_exponential


class LarkImageError(RuntimeError):
    pass


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(min=1, max=8),
    reraise=True,
)
def send_image(
    path: Path,
    *,
    open_id: str,
    receiver_type: Literal["open_id", "chat_id", "user_id", "email"] = "open_id",
    cli_path: str = "lark-cli",
    identity: Literal["bot", "user"] = "bot",
) -> str:
    """Upload `path` and send as an image message. Returns the message_id."""
    if not path.exists():
        raise LarkImageError(f"file not found: {path}")
    args = [
        cli_path, "im", "+messages-send",
        "--receive-id-type", receiver_type,
        "--receive-id", open_id,
        "--type", "image",
        "--content", f"@{path.absolute()}",
        "--as", identity,
        "--format", "json",
    ]
    res = subprocess.run(args, capture_output=True, text=True)
    if res.returncode != 0:
        raise LarkImageError(
            f"lark-cli image send failed (rc={res.returncode}): "
            f"{res.stderr.strip()}"
        )
    try:
        body = json.loads(res.stdout)
    except json.JSONDecodeError as e:
        raise LarkImageError(
            f"unparseable lark-cli response: {res.stdout!r}"
        ) from e
    msg_id = body.get("message_id") or body.get("data", {}).get("message_id")
    if not msg_id:
        raise LarkImageError(f"no message_id in response: {body}")
    return str(msg_id)
```

- [ ] **Step 5: Run test, expect PASS**

```bash
pytest tests/unit/test_reports_lark_image.py -v
```

Expected: 3 PASS.

- [ ] **Step 6: Commit**

```bash
git add src/equity_monitor/reports/lark_image.py tests/unit/test_reports_lark_image.py
git commit -m "feat(p3): lark-cli image-send wrapper with tenacity retry"
```

---

## Task 3: Multi-frequency K-line in `OpenDClient.kline`

**Files:**
- Modify: `src/equity_monitor/futu_client.py`
- Modify: `src/equity_monitor/data/kline.py`
- Test: extend `tests/unit/test_data_kline.py` (or create if absent)

- [ ] **Step 1: Write failing test for the broadened ktype map**

In `tests/unit/test_data_kline.py` (create if missing) add:

```python
from equity_monitor.futu_client import FREQ_TO_KTYPE, FakeFutuClient, Candle
from datetime import datetime, timezone

from equity_monitor.data.kline import fetch_kline_df, fetch_klines_multi


def test_freq_to_ktype_covers_all_user_visible_freqs() -> None:
    assert FREQ_TO_KTYPE == {
        "1m":  "K_1M",
        "5m":  "K_5M",
        "15m": "K_15M",
        "30m": "K_30M",
        "60m": "K_60M",
        "D":   "K_DAY",
        "W":   "K_WEEK",
    }


def test_fetch_klines_multi_returns_one_df_per_requested_freq() -> None:
    client = FakeFutuClient()
    bars = [
        Candle(
            code="US.AAPL",
            ts=datetime(2026, 5, 1, tzinfo=timezone.utc),
            open=1.0, high=2.0, low=0.5, close=1.5,
            volume=10, turnover=15.0,
        )
    ]
    client.set_kline("US.AAPL", "K_DAY", bars)
    client.set_kline("US.AAPL", "K_60M", bars)

    out = fetch_klines_multi(client, "US.AAPL", freqs=["D", "60m"])
    assert set(out.keys()) == {"D", "60m"}
    assert not out["D"].empty
    assert not out["60m"].empty


def test_fetch_klines_multi_skips_unknown_freq() -> None:
    client = FakeFutuClient()
    out = fetch_klines_multi(client, "US.AAPL", freqs=["D", "junk"])
    assert "junk" not in out
```

- [ ] **Step 2: Run test, expect FAIL**

```bash
pytest tests/unit/test_data_kline.py -v
```

Expected: `ImportError: cannot import name 'FREQ_TO_KTYPE'`.

- [ ] **Step 3: Widen `OpenDClient.kline` ktype map**

In `src/equity_monitor/futu_client.py`, near the top of the module (above `class FutuClient`), add:

```python
FREQ_TO_KTYPE: dict[str, str] = {
    "1m":  "K_1M",
    "5m":  "K_5M",
    "15m": "K_15M",
    "30m": "K_30M",
    "60m": "K_60M",
    "D":   "K_DAY",
    "W":   "K_WEEK",
}
```

Inside `OpenDClient.kline()`, replace the line currently reading:

```python
kt = {"K_60M": KLType.K_60M, "K_DAY": KLType.K_DAY}[ktype]
```

with:

```python
kt = {
    "K_1M":   KLType.K_1M,
    "K_5M":   KLType.K_5M,
    "K_15M":  KLType.K_15M,
    "K_30M":  KLType.K_30M,
    "K_60M":  KLType.K_60M,
    "K_DAY":  KLType.K_DAY,
    "K_WEEK": KLType.K_WEEK,
}[ktype]
```

The `lookback_days` heuristic in the same function already pads generously enough for daily / weekly bars; no change needed there.

- [ ] **Step 4: Add `fetch_klines_multi` to `data/kline.py`**

Append to `src/equity_monitor/data/kline.py`:

```python
from equity_monitor.futu_client import FREQ_TO_KTYPE


def fetch_klines_multi(
    client: FutuClient,
    code: str,
    freqs: list[str],
    *,
    limit: int = 200,
) -> dict[str, pd.DataFrame]:
    """Pull all requested freqs in sequence; tolerant of empty returns and
    unknown freq tokens (silently skipped)."""
    out: dict[str, pd.DataFrame] = {}
    for freq in freqs:
        ktype = FREQ_TO_KTYPE.get(freq)
        if ktype is None:
            continue
        out[freq] = fetch_kline_df(client, code, ktype=ktype, limit=limit)
    return out
```

- [ ] **Step 5: Run test, expect PASS**

```bash
pytest tests/unit/test_data_kline.py -v
```

Expected: 3 PASS (plus any pre-existing tests).

- [ ] **Step 6: Commit**

```bash
git add src/equity_monitor/futu_client.py src/equity_monitor/data/kline.py tests/unit/test_data_kline.py
git commit -m "feat(p3): widen kline ktype map (1m/5m/15m/30m/60m/D/W) + fetch_klines_multi"
```

---

## Task 4: Auto-attach K-line snapshot to signal alerts

**Files:**
- Modify: `src/equity_monitor/scheduler/jobs.py`
- Modify: `src/equity_monitor/scheduler/runner.py`
- Test: `tests/integration/test_intraday_alert_with_snapshot.py`

- [ ] **Step 1: Write failing integration test**

Create `tests/integration/test_intraday_alert_with_snapshot.py`. Reuse the existing fixtures style from `tests/integration/test_intraday_check.py` (they build a `FakeFutuClient` with a price near the user threshold and seed the watchlist):

```python
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from equity_monitor.config import (
    AppConfig, DatabaseConfig, LarkConfig, LarkReceiver,
    OpenDConfig, SignalsConfig, WatchlistConfig, WatchSymbol,
)
from equity_monitor.db import init_schema, session_scope
from equity_monitor.futu_client import Candle, FakeFutuClient, Snapshot
from equity_monitor.models import Position, Symbol, Trade
from equity_monitor.scheduler.jobs import run_intraday_check


@pytest.fixture
def factory():
    eng = create_engine("sqlite:///:memory:")
    init_schema(eng)
    return sessionmaker(bind=eng, expire_on_commit=False)


@pytest.fixture
def cfg():
    return AppConfig(
        opend=OpenDConfig(host="127.0.0.1", port=11111),
        database=DatabaseConfig(path=":memory:"),
        signals=SignalsConfig(),
        lark=LarkConfig(receiver=LarkReceiver(open_id="ou_x", type="open_id")),
    )


@pytest.fixture
def watchlist():
    return WatchlistConfig(symbols=[
        WatchSymbol(code="US.AAPL", name="Apple",
                    upper_threshold=300.0, lower_threshold=150.0),
    ])


@pytest.fixture
def fake_client_lower_breach():
    client = FakeFutuClient()
    client.set_snapshot(Snapshot(
        code="US.AAPL", last_price=140.0, open_price=145.0,
        high_price=146.0, low_price=139.5, volume=1000, turnover=140000.0,
        update_time=datetime(2026, 5, 3, 16, 0, tzinfo=timezone.utc),
    ))
    bars = []
    base = datetime(2026, 5, 1, tzinfo=timezone.utc)
    for i in range(100):
        bars.append(Candle(
            code="US.AAPL",
            ts=base + timedelta(hours=i),
            open=200.0 - i * 0.5, high=201.0 - i * 0.5,
            low=199.0 - i * 0.5, close=200.0 - i * 0.5,
            volume=1000, turnover=200_000.0,
        ))
    client.set_kline("US.AAPL", "K_60M", bars)
    return client


def _seed_watchlist(factory, wl):
    with session_scope(factory) as s:
        for sc in wl.symbols:
            s.add(Symbol(
                code=sc.code, name=sc.name,
                upper_threshold=sc.upper_threshold,
                lower_threshold=sc.lower_threshold,
                is_active=True,
            ))


def test_intraday_alert_pushes_card_then_image(
    factory, fake_client_lower_breach, cfg, watchlist, tmp_path,
) -> None:
    _seed_watchlist(factory, watchlist)

    pushed_cards: list = []
    pushed_images: list[Path] = []

    def fake_card_send(card, oid, rt):
        pushed_cards.append(card)
        return "card_msg"

    def fake_image_send(path, oid, rt):
        pushed_images.append(path)
        return "img_msg"

    out = run_intraday_check(
        client=fake_client_lower_breach,
        factory=factory,
        cfg=cfg,
        watchlist=watchlist,
        send_card_fn=fake_card_send,
        send_image_fn=fake_image_send,
        snapshot_dir=tmp_path,
    )

    assert out["pushed"] >= 1, out
    assert pushed_cards, "expected at least one alert card"
    assert pushed_images, "expected a snapshot PNG attached to the alert"
    assert pushed_images[0].suffix == ".png"
    assert pushed_images[0].exists()


def test_intraday_skips_image_when_image_sender_is_none(
    factory, fake_client_lower_breach, cfg, watchlist, tmp_path,
) -> None:
    _seed_watchlist(factory, watchlist)
    pushed_cards: list = []

    out = run_intraday_check(
        client=fake_client_lower_breach,
        factory=factory,
        cfg=cfg,
        watchlist=watchlist,
        send_card_fn=lambda c, o, r: (pushed_cards.append(c), "msg")[1],
        send_image_fn=None,           # ← off
        snapshot_dir=tmp_path,
    )

    assert out["pushed"] >= 1
    assert pushed_cards
    # Nothing rendered, nothing pushed.
    assert list(tmp_path.glob("*.png")) == []


def test_intraday_image_pushes_with_trade_markers(
    factory, fake_client_lower_breach, cfg, watchlist, tmp_path,
) -> None:
    """If we already have a Trade for the symbol, the snapshot must include it."""
    _seed_watchlist(factory, watchlist)
    with session_scope(factory) as s:
        sym = s.query(Symbol).filter(Symbol.code == "US.AAPL").one()
        s.add(Trade(
            symbol_id=sym.id,
            ts=datetime(2026, 5, 2, tzinfo=timezone.utc),
            side="BUY", qty=100, price=180.0, status="FILLED",
        ))
        s.add(Position(symbol_id=sym.id, qty=100, avg_cost=180.0))

    captured_images: list[Path] = []

    out = run_intraday_check(
        client=fake_client_lower_breach,
        factory=factory,
        cfg=cfg,
        watchlist=watchlist,
        send_card_fn=lambda c, o, r: "msg",
        send_image_fn=lambda p, o, r: (captured_images.append(p), "msg")[1],
        snapshot_dir=tmp_path,
    )

    assert out["pushed"] >= 1
    assert captured_images, "expected snapshot PNG"
    assert captured_images[0].stat().st_size > 1024
```

- [ ] **Step 2: Run test, expect FAIL**

```bash
pytest tests/integration/test_intraday_alert_with_snapshot.py -v
```

Expected: FAIL with `TypeError: run_intraday_check() got an unexpected keyword argument 'send_image_fn'`.

- [ ] **Step 3: Plumb snapshot send through `run_intraday_check`**

In `src/equity_monitor/scheduler/jobs.py` add imports near the top:

```python
from datetime import timedelta
from pathlib import Path

from equity_monitor.reports.lark_image import LarkImageError, send_image as default_send_image
from equity_monitor.reports.snapshot import (
    SnapshotRequest,
    TradeMarker,
    render_snapshot,
)
```

Add a sender alias type and a default at module scope:

```python
SendImageFn = Callable[[Path, str, str], str]


def _default_image_sender(path: Path, oid: str, rt: str) -> str:
    return default_send_image(path, open_id=oid, receiver_type=rt)  # type: ignore[arg-type]
```

Extend the `run_intraday_check` signature (around line 152):

```python
def run_intraday_check(
    *,
    client: FutuClient,
    factory: sessionmaker,
    cfg: AppConfig,
    watchlist: WatchlistConfig,
    now_utc: datetime | None = None,
    send_card_fn: SendCardFn = _default_sender,
    send_image_fn: SendImageFn | None = None,
    snapshot_dir: Path | None = None,
) -> dict[str, int]:
```

Inside the loop where `ind_df` per symbol is computed, capture it for later use. There is already a `for sym_cfg in watchlist.symbols:` block that computes `df = fetch_kline_df(...)` and `ind_df = compute_indicators(df, ...)`. Add a per-code stash before the existing logic mutates `last`:

```python
ind_df_by_code: dict[str, pd.DataFrame] = {}
# ...inside the loop, after ind_df is computed:
ind_df_by_code[sym_cfg.code] = ind_df
```

Then in `_push_for_code` (the inner closure), AFTER the successful `send_card_fn(...)` call, append:

```python
        if send_image_fn is None:
            return
        try:
            ohlcv_df = ind_df_by_code.get(code)
            if ohlcv_df is None or ohlcv_df.empty:
                return
            df_for_chart = ohlcv_df.loc[
                :, ["open", "high", "low", "close", "volume"]
            ].tail(120)

            with session_scope(factory) as s2:
                sym = s2.query(Symbol).filter(Symbol.code == code).one_or_none()
                markers: list[TradeMarker] = []
                if sym is not None:
                    cutoff = ts_for_card - timedelta(days=30)
                    rows = (
                        s2.query(Trade)
                        .filter(Trade.symbol_id == sym.id, Trade.ts >= cutoff)
                        .all()
                    )
                    for t in rows:
                        markers.append(TradeMarker(
                            ts=t.ts,
                            side="buy" if t.side.upper() == "BUY" else "sell",
                            qty=t.qty,
                            price=t.price,
                        ))

            avg_cost: float | None = None
            if code in positions_by_code:
                _, avg_cost_value = positions_by_code[code]
                avg_cost = avg_cost_value if avg_cost_value > 0 else None
            current_price = (
                snapshots_by_code[code].last_price
                if code in snapshots_by_code else None
            )
            req = SnapshotRequest(
                code=code,
                freq="60m",
                df=df_for_chart,
                markers=markers,
                avg_cost=avg_cost,
                current_price=current_price,
                out_dir=snapshot_dir,
            )
            png_path = render_snapshot(req)
            send_image_fn(
                png_path, cfg.lark.receiver.open_id, cfg.lark.receiver.type
            )
            log.info("intraday_check.snapshot_pushed", code=code, path=str(png_path))
        except Exception as e:  # snapshot failure must not block alerts
            log.error("intraday_check.snapshot_failed", code=code, error=str(e))
```

- [ ] **Step 4: Wire the default image sender in the scheduler runner**

In `src/equity_monitor/scheduler/runner.py`, find the `scheduler.add_job(run_intraday_check, ...)` call. Update its `kwargs` dict to include the default image sender and a snapshot directory:

```python
from pathlib import Path
from equity_monitor.scheduler.jobs import _default_image_sender

# ...inside run_forever, where intraday_check is registered:
scheduler.add_job(
    run_intraday_check,
    CronTrigger(...),
    kwargs={
        "client": client,
        "factory": factory,
        "cfg": cfg,
        "watchlist": watchlist,
        "send_image_fn": _default_image_sender,
        "snapshot_dir": Path("var/snapshots"),
    },
    id="intraday_check",
)
```

The `cli/main.py:once intraday` invocation does NOT need to be updated; defaulting `send_image_fn=None` keeps `equity-monitor once --job intraday` non-spammy for ad-hoc test runs.

- [ ] **Step 5: Run test, expect PASS**

```bash
pytest tests/integration/test_intraday_alert_with_snapshot.py -v
pytest tests/integration/ -q  # verify no regressions
```

Expected: 3 new PASS + all existing integration tests still PASS.

- [ ] **Step 6: Commit**

```bash
git add src/equity_monitor/scheduler/jobs.py src/equity_monitor/scheduler/runner.py tests/integration/test_intraday_alert_with_snapshot.py
git commit -m "feat(p3): auto-attach K-line snapshot PNG to signal alerts"
```

---

## Task 5: `/chart` listener command

**Files:**
- Modify: `src/equity_monitor/events/grammar.py`
- Modify: `src/equity_monitor/events/apply.py`
- Modify: `src/equity_monitor/events/listener.py`
- Modify: `src/equity_monitor/cli/main.py` (for an ad-hoc `chart` shell command)
- Test: `tests/unit/test_event_grammar_chart.py`
- Test: `tests/integration/test_listener_chart.py`

- [ ] **Step 1: Failing grammar test**

Create `tests/unit/test_event_grammar_chart.py`:

```python
import pytest

from equity_monitor.events.grammar import ChartCommand, ParseError, parse


def test_chart_with_default_freq() -> None:
    [c] = parse("/chart US.AAPL")
    assert isinstance(c, ChartCommand)
    assert c.code == "US.AAPL"
    assert c.freq == "60m"


def test_chart_with_explicit_freq_day() -> None:
    [c] = parse("/chart US.AAPL D")
    assert c.freq == "D"


def test_chart_with_explicit_freq_5m() -> None:
    [c] = parse("/chart US.AAPL 5m")
    assert c.freq == "5m"


def test_chart_lowercase_code_normalized_to_upper() -> None:
    [c] = parse("/chart us.aapl 60m")
    assert c.code == "US.AAPL"


def test_chart_unknown_freq_falls_back_to_default() -> None:
    [c] = parse("/chart US.AAPL Q")
    assert c.freq == "60m"


def test_chart_no_code_raises_parse_error() -> None:
    with pytest.raises(ParseError, match="code"):
        parse("/chart")
```

- [ ] **Step 2: Run, expect FAIL**

```bash
pytest tests/unit/test_event_grammar_chart.py -v
```

Expected: `ImportError: cannot import name 'ChartCommand'`.

- [ ] **Step 3: Add `ChartCommand` to `events/grammar.py`**

Edit `src/equity_monitor/events/grammar.py`. Near the top of the file (next to existing dataclass commands), add:

```python
ALLOWED_CHART_FREQS: frozenset[str] = frozenset({"5m", "15m", "30m", "60m", "D", "W"})


@dataclass(frozen=True)
class ChartCommand:
    code: str
    freq: str = "60m"
```

Update the `Command` union to include `ChartCommand`.

Add the parser:

```python
def _parse_chart(rest: str) -> ChartCommand:
    parts = [p for p in rest.split() if p]
    if not parts:
        raise ParseError("/chart 需要 code，如 /chart US.AAPL D")
    code = parts[0].upper()
    if not _CODE_RE.fullmatch(code):
        raise ParseError(f"非法代码 {parts[0]!r}")
    freq = parts[1] if len(parts) > 1 else "60m"
    if freq not in ALLOWED_CHART_FREQS:
        freq = "60m"
    return ChartCommand(code=code, freq=freq)
```

In the existing `parse()` dispatch table (matching on the leading `/word`), add a `chart` case calling `_parse_chart`. Update `HELP_TEXT` (or the equivalent) in `events/apply.py` to include the new command.

- [ ] **Step 4: Run grammar test, expect PASS**

```bash
pytest tests/unit/test_event_grammar_chart.py -v
```

Expected: 6 PASS.

- [ ] **Step 5: Add chart handler to `events/apply.py`**

Add at the top of `src/equity_monitor/events/apply.py`:

```python
from datetime import datetime, timedelta, timezone
from pathlib import Path

from equity_monitor.data.kline import fetch_kline_df
from equity_monitor.events.grammar import ChartCommand
from equity_monitor.futu_client import FREQ_TO_KTYPE, FutuClient
from equity_monitor.models import Position, Symbol, Trade
from equity_monitor.reports.snapshot import (
    SnapshotRequest,
    TradeMarker,
    render_snapshot,
)


@dataclass(frozen=True)
class ChartReplyPayload:
    """Returned alongside the text reply when an apply produces a chart PNG."""
    image_path: Path
```

Add the handler function (alongside existing `_apply_*` helpers):

```python
def _apply_chart(
    cmd: ChartCommand,
    session: Any,
    *,
    client: FutuClient,
    snapshot_dir: Path | None = None,
) -> tuple[str, ChartReplyPayload | None]:
    sym = (
        session.query(Symbol)
        .filter(Symbol.code == cmd.code)
        .one_or_none()
    )
    if sym is None:
        return f"⚠️ {cmd.code} 不在 watchlist", None

    ktype = FREQ_TO_KTYPE[cmd.freq]
    df = fetch_kline_df(client, cmd.code, ktype=ktype, limit=200)
    if df.empty:
        return f"⚠️ 无 {cmd.code} {cmd.freq} K 线数据", None

    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=90)
    trades = (
        session.query(Trade)
        .filter(Trade.symbol_id == sym.id, Trade.ts >= cutoff)
        .all()
    )
    markers = [
        TradeMarker(
            ts=t.ts,
            side="buy" if t.side.upper() == "BUY" else "sell",
            qty=t.qty,
            price=t.price,
        )
        for t in trades
    ]

    pos = (
        session.query(Position)
        .filter(Position.symbol_id == sym.id, Position.qty > 0)
        .one_or_none()
    )
    avg_cost = pos.avg_cost if pos else None

    snaps = client.snapshot([cmd.code])
    current_price = snaps[0].last_price if snaps else None

    req = SnapshotRequest(
        code=cmd.code,
        freq=cmd.freq,
        df=df,
        markers=markers,
        avg_cost=avg_cost,
        current_price=current_price,
        out_dir=snapshot_dir,
    )
    png = render_snapshot(req)

    text = (
        f"📈 {cmd.code} · {cmd.freq}"
        + (f"  ${current_price:.2f}" if current_price else "")
        + (f" (avg ${avg_cost:.2f})" if avg_cost else "")
        + f" · {len(markers)} trade(s) in last 90d"
    )
    return text, ChartReplyPayload(image_path=png)
```

In the `apply()` dispatch (the function that takes a `Command` and routes by isinstance), add:

```python
if isinstance(cmd, ChartCommand):
    return _apply_chart(cmd, session, client=client, snapshot_dir=snapshot_dir)
```

The signature of `apply()` likely needs the new optional kwargs `client` and `snapshot_dir`. Add them with defaults of `None` so existing callers still work; the listener will pass them through.

- [ ] **Step 6: Listener routes the image**

In `src/equity_monitor/events/listener.py`, locate `dispatch_event` (or whatever the in-memory routing function is called — see existing code for `events/listener.py`). After the apply call returns `(text, payload)`:

```python
text, payload = apply(cmd, session, client=client, snapshot_dir=snapshot_dir)
# Existing: send the text/card reply.
send_text(text, ...)
if isinstance(payload, ChartReplyPayload):
    try:
        send_image_fn(
            payload.image_path,
            cfg.lark.receiver.open_id,
            cfg.lark.receiver.type,
        )
    except Exception as e:
        log.error("listener.chart_image_failed", error=str(e))
```

Add `send_image_fn` (defaulting to `_default_image_sender` from `scheduler/jobs.py` or to the `lark_image.send_image` helper directly) to `run_listener`'s signature and through to `dispatch_event`.

- [ ] **Step 7: Failing integration test**

Create `tests/integration/test_listener_chart.py`:

```python
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from equity_monitor.db import init_schema, session_scope
from equity_monitor.events.apply import ChartReplyPayload, apply
from equity_monitor.events.grammar import parse
from equity_monitor.futu_client import Candle, FakeFutuClient, Snapshot
from equity_monitor.models import Position, Symbol, Trade


@pytest.fixture
def factory(tmp_path):
    eng = create_engine("sqlite:///:memory:")
    init_schema(eng)
    return sessionmaker(bind=eng, expire_on_commit=False)


@pytest.fixture
def client_with_kline_and_snap():
    client = FakeFutuClient()
    client.set_snapshot(Snapshot(
        code="US.AAPL", last_price=190.0, open_price=185.0,
        high_price=192.0, low_price=184.0, volume=1000,
        turnover=190_000.0,
        update_time=datetime(2026, 5, 3, tzinfo=timezone.utc),
    ))
    base = datetime(2026, 4, 1, tzinfo=timezone.utc)
    bars = [Candle(
        code="US.AAPL", ts=base + timedelta(days=i),
        open=180.0 + i, high=181.0 + i,
        low=179.0 + i, close=180.5 + i,
        volume=1000, turnover=180_500.0,
    ) for i in range(30)]
    client.set_kline("US.AAPL", "K_DAY", bars)
    client.set_kline("US.AAPL", "K_60M", bars)
    return client


def test_chart_command_renders_png_for_known_symbol(
    factory, client_with_kline_and_snap, tmp_path,
) -> None:
    with session_scope(factory) as s:
        s.add(Symbol(code="US.AAPL", name="Apple",
                     upper_threshold=None, lower_threshold=None,
                     is_active=True))
        s.flush()
        sid = s.query(Symbol).filter(Symbol.code == "US.AAPL").one().id
        s.add(Trade(
            symbol_id=sid, ts=datetime(2026, 4, 5, tzinfo=timezone.utc),
            side="BUY", qty=100, price=181.0, status="FILLED",
        ))
        s.add(Position(symbol_id=sid, qty=100, avg_cost=181.0))

    [cmd] = parse("/chart US.AAPL D")
    with session_scope(factory) as s:
        text, payload = apply(
            cmd, s,
            client=client_with_kline_and_snap,
            snapshot_dir=tmp_path,
        )
    assert "US.AAPL" in text
    assert "1 trade" in text
    assert isinstance(payload, ChartReplyPayload)
    assert payload.image_path.exists()
    assert payload.image_path.suffix == ".png"


def test_chart_command_rejects_unknown_symbol(
    factory, client_with_kline_and_snap, tmp_path,
) -> None:
    [cmd] = parse("/chart US.UNKNOWN")
    with session_scope(factory) as s:
        text, payload = apply(
            cmd, s,
            client=client_with_kline_and_snap,
            snapshot_dir=tmp_path,
        )
    assert "不在 watchlist" in text
    assert payload is None


def test_chart_command_handles_empty_kline(
    factory, tmp_path,
) -> None:
    client = FakeFutuClient()
    client.set_snapshot(Snapshot(
        code="US.AAPL", last_price=190.0, open_price=185.0,
        high_price=192.0, low_price=184.0, volume=1,
        turnover=190.0,
        update_time=datetime(2026, 5, 3, tzinfo=timezone.utc),
    ))
    # No klines registered → fake client returns []

    with session_scope(factory) as s:
        s.add(Symbol(code="US.AAPL", name="Apple",
                     upper_threshold=None, lower_threshold=None,
                     is_active=True))

    [cmd] = parse("/chart US.AAPL D")
    with session_scope(factory) as s:
        text, payload = apply(cmd, s, client=client, snapshot_dir=tmp_path)
    assert "无 US.AAPL D K 线数据" in text
    assert payload is None
```

- [ ] **Step 8: Run, expect PASS**

```bash
pytest tests/integration/test_listener_chart.py -v
```

Expected: 3 PASS.

- [ ] **Step 9: Add an ad-hoc CLI `chart` command**

In `src/equity_monitor/cli/main.py`, append at the end (before `if __name__ == "__main__":`):

```python
@cli.command()
@click.argument("code")
@click.option(
    "--freq", default="60m", show_default=True,
    type=click.Choice(["5m", "15m", "30m", "60m", "D", "W"]),
)
@click.option(
    "--out-dir", default="var/snapshots", show_default=True,
    type=click.Path(),
)
@click.option("--push/--no-push", default=False, show_default=True,
              help="Also push the PNG to Lark.")
@click.pass_context
def chart(ctx: click.Context, code: str, freq: str, out_dir: str, push: bool) -> None:
    """Render an ad-hoc K-line snapshot PNG (and optionally push to Lark)."""
    from datetime import datetime, timedelta, timezone

    from equity_monitor.data.kline import fetch_kline_df
    from equity_monitor.futu_client import FREQ_TO_KTYPE
    from equity_monitor.reports.snapshot import (
        SnapshotRequest, TradeMarker, render_snapshot,
    )
    from equity_monitor.reports.lark_image import send_image

    cfg = _get_cfg(ctx)
    factory = _make_factory(cfg)
    client = OpenDClient(cfg.opend.host, cfg.opend.port)
    try:
        df = fetch_kline_df(
            client, code.upper(), ktype=FREQ_TO_KTYPE[freq], limit=200,
        )
        markers: list[TradeMarker] = []
        avg_cost = None
        with session_scope(factory) as s:
            sym = s.query(Symbol).filter(Symbol.code == code.upper()).one_or_none()
            if sym is not None:
                cutoff = datetime.now(tz=timezone.utc) - timedelta(days=90)
                for t in s.query(Trade).filter(
                    Trade.symbol_id == sym.id, Trade.ts >= cutoff,
                ):
                    markers.append(TradeMarker(
                        ts=t.ts,
                        side="buy" if t.side.upper() == "BUY" else "sell",
                        qty=t.qty, price=t.price,
                    ))
                pos = (
                    s.query(Position)
                    .filter(Position.symbol_id == sym.id, Position.qty > 0)
                    .one_or_none()
                )
                avg_cost = pos.avg_cost if pos else None
        snaps = client.snapshot([code.upper()])
        current_price = snaps[0].last_price if snaps else None
        req = SnapshotRequest(
            code=code.upper(), freq=freq, df=df,
            markers=markers, avg_cost=avg_cost,
            current_price=current_price,
            out_dir=Path(out_dir),
        )
        png = render_snapshot(req)
    finally:
        client.close()
    click.echo(str(png))
    if push:
        msg_id = send_image(
            png,
            open_id=cfg.lark.receiver.open_id,
            receiver_type=cfg.lark.receiver.type,
        )
        click.echo(f"pushed: msg_id={msg_id}")
```

- [ ] **Step 10: Smoke + Commit**

Smoke locally with OpenD running:

```bash
equity-monitor chart US.AAPL --freq D
# expect: var/snapshots/US_AAPL_D_<ts>.png
equity-monitor chart US.AAPL --freq D --push
# expect: file path printed, then "pushed: msg_id=..."
```

Then:

```bash
git add src/equity_monitor/events/grammar.py src/equity_monitor/events/apply.py src/equity_monitor/events/listener.py src/equity_monitor/cli/main.py tests/unit/test_event_grammar_chart.py tests/integration/test_listener_chart.py
git commit -m "feat(p3): /chart listener + CLI command for on-demand K-line snapshot"
```

---

## Task 6: Documentation + smoke script

**Files:**
- Modify: `README.md`
- Modify: `docs/lark_app_description.md`
- Create: `scripts/smoke_phase3.py`

- [ ] **Step 1: README update**

In `README.md` under the Phase 2.5 section, append:

````markdown
## Phase 3 (scoped) — K-line snapshot visualization

Static K-line PNG snapshots, delivered to Lark.

What's new:
- **Auto-attached snapshots**: every signal alert now ships a 60-min
  K-line PNG with your BUY/SELL markers (last 30 days) + average-cost
  line + current-price line.
- **`/chart <code> [freq]` listener command**: pull a snapshot on demand.
  Freq options: `5m / 15m / 30m / 60m / D / W` (default `60m`).
- **`equity-monitor chart <code>` CLI**: same renderer for shell use.

```bash
# render only (offline-safe; needs OpenD on 11111 for data)
equity-monitor chart US.AAPL --freq D

# render and push to Lark
equity-monitor chart US.AAPL --freq D --push
```

Snapshots land under `var/snapshots/`. The directory is gitignored except
for the placeholder `.gitkeep`.

Out of scope for this Phase 3 increment (parked for later):
- Strategy abstraction layer + per-strategy P&L / max-drawdown
- `/positions`, `/pnl`, `/history` listener commands
- QuantStats weekly tearsheet
- BackfillState cursor for incremental K-line pulls
````

- [ ] **Step 2: Lark command reference**

In `docs/lark_app_description.md`, append to the command list:

```markdown
- `/chart <code> [freq]` — render a K-line PNG snapshot with your trade
  markers and cost line. `freq` ∈ {5m, 15m, 30m, 60m, D, W}; defaults to 60m.
```

- [ ] **Step 3: Smoke script**

Create `scripts/smoke_phase3.py`:

```python
"""Phase 3 smoke. Requires OpenD on 11111 + a healthy lark-cli auth.

Side effects:
1. equity-monitor once --job intraday → may push a card + image to Lark.
2. Render a chart snapshot for the first watchlist symbol → PNG on disk.
3. Optionally push the chart to Lark when --push is given.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import click

from equity_monitor.cli.main import _get_cfg, _get_watchlist, _make_factory
from equity_monitor.data.kline import fetch_kline_df
from equity_monitor.futu_client import FREQ_TO_KTYPE, OpenDClient
from equity_monitor.reports.lark_image import send_image
from equity_monitor.reports.snapshot import SnapshotRequest, render_snapshot
from equity_monitor.scheduler.jobs import _default_image_sender, run_intraday_check


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--push", action="store_true")
    p.add_argument("--freq", default="D", choices=list(FREQ_TO_KTYPE.keys()))
    args = p.parse_args()

    ctx = click.Context(click.Command(""), obj={
        "settings_path": "config/settings.yaml",
        "watchlist_path": "config/watchlist.yaml",
    })
    cfg = _get_cfg(ctx)
    wl = _get_watchlist(ctx)
    factory = _make_factory(cfg)
    client = OpenDClient(cfg.opend.host, cfg.opend.port)
    try:
        out = run_intraday_check(
            client=client, factory=factory, cfg=cfg, watchlist=wl,
            send_image_fn=_default_image_sender if args.push else None,
            snapshot_dir=Path("var/snapshots"),
        )
        print("intraday:", out)

        sym = wl.symbols[0]
        df = fetch_kline_df(
            client, sym.code, ktype=FREQ_TO_KTYPE[args.freq], limit=200,
        )
        png = render_snapshot(SnapshotRequest(
            code=sym.code, freq=args.freq, df=df,
            markers=[], avg_cost=None, current_price=None,
            out_dir=Path("var/snapshots"),
        ))
        print("snapshot:", png)
        if args.push:
            msg_id = send_image(
                png,
                open_id=cfg.lark.receiver.open_id,
                receiver_type=cfg.lark.receiver.type,
            )
            print("pushed:", msg_id)
    finally:
        client.close()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run smoke (manual)**

```bash
python scripts/smoke_phase3.py --freq D --push
```

Expected: intraday result dict prints, then a PNG path, then a Lark message_id.

- [ ] **Step 5: Run full test suite + commit**

```bash
pytest -q
git add README.md docs/lark_app_description.md scripts/smoke_phase3.py
git commit -m "docs(p3): README + lark reference + smoke_phase3 script"
git tag -a phase-3-snapshots-mvp -m "Phase 3 (scoped) — K-line snapshot visualization MVP"
```

---

## Self-Review Checklist

| Requirement | Implementing task |
|---|---|
| K-line viz tool with my BUY/SELL markers | Task 1 (`mplfinance`) + Task 4 (auto attach) + Task 5 (`/chart`) |
| Multi-frequency (not fixed daily K) | Task 3 (`FREQ_TO_KTYPE` widening) + Task 5 (`/chart` accepts `5m/15m/30m/60m/D/W`) |
| Mobile-readable | All snapshots are PNG → render natively in Lark mobile app |
| 持仓/P&L/历史 仍走飞书卡片 | Untouched: existing daily-brief & signal-alert cards still carry P&L lines |
| 策略抽象 + 投资周期不固定 | **Deferred to a separate plan** (out-of-scope here, callout in README) |

**Placeholder scan**: search this plan for `TBD`, `TODO`, `implement later`, `Add appropriate`, `Similar to Task` — there should be zero hits.

**Type consistency**:
- `TradeMarker.side` is the lowercase literal `"buy" | "sell"` everywhere it appears (Task 1 dataclass, Task 4 trade-row mapping, Task 5 chart handler).
- `SnapshotRequest` field names: `code / freq / df / markers / avg_cost / current_price / out_dir` — identical in Tasks 1, 4, 5.
- `FREQ_TO_KTYPE` lives in `futu_client.py` (Task 3) and is imported by Tasks 5 + 6.
- `ChartReplyPayload.image_path` is a `pathlib.Path` everywhere (Task 5 + listener routing).
- `SendImageFn = Callable[[Path, str, str], str]` — argument order is **(path, open_id, receiver_type)** for the bare callback. This differs from `lark_image.send_image`'s keyword-only signature; the wrapper `_default_image_sender` does the adaptation in Task 4.

If any rename happens during implementation, propagate everywhere.

---

## Total work estimate

| Task | Hours |
|---|---|
| 1. Snapshot renderer | 4 |
| 2. lark-cli image sender | 2 |
| 3. Multi-freq kline widening | 1.5 |
| 4. Auto-attach to signal alert | 4 |
| 5. `/chart` listener + CLI | 4 |
| 6. Docs + smoke | 2 |

**Total ≈ 17.5 hours / 2.2 working days.**

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-05-03-equity-monitor-phase3-observability.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
