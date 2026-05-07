"""End-to-end smoke test against real OpenD + lark-cli.

Prerequisites:
  - OpenD running on 127.0.0.1:11111 (logged in)
  - lark-cli on PATH and authed
  - config/watchlist.yaml + config/settings.yaml populated
  - Symbols are synced into DB (run `vibe-trader watchlist sync` first)

Run:
    python scripts/smoke_e2e.py

What it does:
  1. Loads settings + watchlist
  2. Connects to OpenD
  3. Runs all three jobs sequentially:
       - intraday_check
       - morning_brief
       - closing_brief
  4. Prints the per-job result dict
  5. You should see 3 Interactive Cards in your Lark IM (one per job)

Exits non-zero on the first exception (so you see a clear failure mode).
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from vibe_trader.config import load_settings, load_watchlist  # noqa: E402
from vibe_trader.db import init_schema, make_engine, make_sessionmaker  # noqa: E402
from vibe_trader.futu_client import OpenDClient  # noqa: E402
from vibe_trader.scheduler.jobs import (  # noqa: E402
    run_closing_brief,
    run_intraday_check,
    run_morning_brief,
)


def main() -> int:
    cfg_path = ROOT / "config" / "settings.yaml"
    wl_path = ROOT / "config" / "watchlist.yaml"
    if not cfg_path.exists() or not wl_path.exists():
        print(
            f"missing config: {cfg_path} or {wl_path}\n"
            "  → run `cp config/watchlist.example.yaml config/watchlist.yaml`\n"
            "    and edit settings.yaml first.",
            file=sys.stderr,
        )
        return 2

    cfg = load_settings(cfg_path)
    wl = load_watchlist(wl_path)

    Path(cfg.database.path).parent.mkdir(parents=True, exist_ok=True)
    engine = make_engine(cfg.database.path, wal_mode=cfg.database.wal_mode)
    init_schema(engine)
    factory = make_sessionmaker(engine)

    print(f"=== smoke_e2e ===")
    print(f"db.path:    {cfg.database.path}")
    print(f"opend:      {cfg.opend.host}:{cfg.opend.port}")
    print(f"watchlist:  {[s.code for s in wl.symbols]}")
    print(f"lark.recv:  {cfg.lark.receiver.type} → {cfg.lark.receiver.open_id}")
    print()

    failures: list[str] = []
    client = OpenDClient(cfg.opend.host, cfg.opend.port)
    try:
        for label, fn in [
            ("intraday", lambda: run_intraday_check(client=client, factory=factory, cfg=cfg, watchlist=wl)),
            ("morning", lambda: run_morning_brief(client=client, factory=factory, cfg=cfg, watchlist=wl)),
            ("closing", lambda: run_closing_brief(client=client, factory=factory, cfg=cfg, watchlist=wl)),
        ]:
            print(f"--- {label} ---")
            try:
                out = fn()
                print(f"  result: {out}")
            except Exception as e:
                failures.append(f"{label}: {e}")
                print(f"  FAILED: {e}")
                traceback.print_exc()
    finally:
        client.close()

    print()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("ALL OK — check your Lark IM for 4 Interactive Cards.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
