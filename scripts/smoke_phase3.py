"""Phase 3 smoke: render a K-line snapshot via apply_chart, optionally
push it to Lark.

Usage:
    conda activate fin
    python scripts/smoke_phase3.py                       # render only
    python scripts/smoke_phase3.py --push                # also push to Lark
    python scripts/smoke_phase3.py --code US.TSLA --freq D --push
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from vibe_trader.config import load_settings  # noqa: E402
from vibe_trader.db import init_schema, make_engine, make_sessionmaker  # noqa: E402
from vibe_trader.events.apply import apply_chart  # noqa: E402
from vibe_trader.events.grammar import (  # noqa: E402
    ALLOWED_CHART_FREQS,
    ChartCommand,
    _normalize_code,
)
from vibe_trader.futu_client import OpenDClient  # noqa: E402
from vibe_trader.reports.lark_image import send_image  # noqa: E402


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Phase 3 smoke (K-line snapshot)")
    p.add_argument("--code", default="US.AAPL")
    p.add_argument(
        "--freq",
        default="60m",
        choices=sorted(ALLOWED_CHART_FREQS),
    )
    p.add_argument(
        "--push",
        action="store_true",
        help="Also push the PNG to the configured Lark recipient.",
    )
    p.add_argument(
        "--out-dir",
        default="var/snapshots",
        help="Where to write the PNG.",
    )
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    cfg_path = ROOT / "config" / "settings.yaml"
    if not cfg_path.exists():
        print(f"missing config: {cfg_path}", file=sys.stderr)
        return 2

    args = _parse_args(argv)
    cfg = load_settings(cfg_path)

    Path(cfg.database.path).parent.mkdir(parents=True, exist_ok=True)
    engine = make_engine(cfg.database.path, wal_mode=cfg.database.wal_mode)
    init_schema(engine)
    factory = make_sessionmaker(engine)

    cmd = ChartCommand(code=_normalize_code(args.code.strip()), freq=args.freq)
    out_dir = Path(args.out_dir).resolve()
    client = OpenDClient(cfg.opend.host, cfg.opend.port)
    try:
        text, payload = apply_chart(
            cmd,
            factory,
            client=client,
            snapshot_dir=out_dir,
        )
    finally:
        try:
            client.close()
        except Exception:
            pass

    num_markers: int | None = None
    m = re.search(r"(\d+) 笔近", text)
    if m:
        num_markers = int(m.group(1))

    print(f"summary: code={cmd.code} freq={cmd.freq} markers={num_markers} path={payload.image_path}")
    print(text)
    print(f"snapshot: {payload.image_path}")

    if args.push:
        try:
            msg_id = send_image(
                payload.image_path,
                open_id=cfg.lark.receiver.open_id,
                receiver_type=cfg.lark.receiver.type,
                cli_path=cfg.lark.cli_path,
                identity=cfg.lark.identity,
            )
        except Exception as e:
            print(f"push FAILED: {e}", file=sys.stderr)
            return 1
        print(f"pushed: {msg_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
