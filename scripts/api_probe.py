"""Full Futu OpenD capability probe for the vibe-trader account.

Verifies that every API surface used by Phase 1 + Phase 2 is reachable on
the live OpenD instance and reports what the user's account can actually do.
"""

from __future__ import annotations

import json
import sys
from contextlib import suppress

CODES = ["US.AAPL", "US.NVDA"]


def _section(title: str) -> None:
    print(f"\n{'=' * 8} {title} {'=' * 8}")


def _result(name: str, ok: bool, detail: str = "") -> None:
    icon = "OK " if ok else "FAIL"
    extra = f" — {detail}" if detail else ""
    print(f"  [{icon}] {name}{extra}")


def probe_quote() -> None:
    """OpenQuoteContext: snapshots, K-line, news search, sentiment."""
    from futu import (  # type: ignore[import-not-found]
        AuType,
        KLType,
        OpenQuoteContext,
        SubType,
    )

    ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
    try:
        _section("Quote: market snapshot")
        ret, data = ctx.get_market_snapshot(CODES)
        _result(
            "get_market_snapshot",
            ret == 0,
            f"{len(data)} rows" if ret == 0 else str(data),
        )
        if ret == 0:
            row = data.iloc[0]
            print(f"        sample: {row['code']} last={row['last_price']} "
                  f"open={row['open_price']} vol={row['volume']}")

        _section("Quote: subscribe + realtime quote")
        ret, msg = ctx.subscribe(CODES, [SubType.QUOTE])
        _result("subscribe(QUOTE)", ret == 0, str(msg) if ret != 0 else "")
        if ret == 0:
            ret2, qdata = ctx.get_stock_quote(CODES)
            _result(
                "get_stock_quote",
                ret2 == 0,
                f"{len(qdata)} rows" if ret2 == 0 else str(qdata),
            )

        _section("Quote: 60-min K-line history")
        ret, kdata, _ = ctx.request_history_kline(
            "US.AAPL", ktype=KLType.K_60M, max_count=20, autype=AuType.QFQ
        )
        _result(
            "request_history_kline K_60M",
            ret == 0,
            f"{len(kdata)} bars" if ret == 0 else str(kdata),
        )
        if ret == 0 and len(kdata):
            print(f"        latest bar: {kdata.iloc[-1].to_dict()}")

        _section("Quote: news search (subscription required)")
        # The futu-api `news` family lives under several names depending on SDK
        # version. Try the documented one first.
        for fn_name in ("get_stock_news", "get_news"):
            fn = getattr(ctx, fn_name, None)
            if fn is None:
                continue
            with suppress(Exception):
                ret, ndata = fn("US.AAPL")
                _result(
                    f"{fn_name}",
                    ret == 0,
                    (f"{len(ndata)} rows" if hasattr(ndata, "__len__") else str(ndata))
                    if ret == 0
                    else str(ndata),
                )
                break
        else:
            _result("news API", False, "no get_stock_news / get_news on this SDK")

        _section("Quote: capital flow / distribution / order book")
        for attr in (
            "get_capital_flow",
            "get_capital_distribution",
            "get_order_book",
            "get_rt_data",
        ):
            fn = getattr(ctx, attr, None)
            if fn is None:
                _result(attr, False, "missing on SDK")
                continue
            try:
                ret, data = fn("US.AAPL")
                _result(
                    attr,
                    ret == 0,
                    f"{len(data) if hasattr(data, '__len__') else 'ok'} rows"
                    if ret == 0
                    else str(data),
                )
            except TypeError:
                # Some need different args
                try:
                    ret, data = fn(["US.AAPL"])
                    _result(attr, ret == 0, "ok" if ret == 0 else str(data))
                except Exception as e:
                    _result(attr, False, str(e))
            except Exception as e:
                _result(attr, False, str(e))
    finally:
        ctx.close()


def probe_trade() -> None:
    """OpenSecTradeContext: paper account, place_order, query positions."""
    from futu import (  # type: ignore[import-not-found]
        OpenSecTradeContext,
        SecurityFirm,
        TrdEnv,
        TrdMarket,
    )

    ctx = OpenSecTradeContext(
        filter_trdmarket=TrdMarket.US,
        host="127.0.0.1",
        port=11111,
        security_firm=SecurityFirm.FUTUSECURITIES,
    )
    try:
        _section("Trade: account list")
        ret, data = ctx.get_acc_list()
        _result("get_acc_list", ret == 0, str(data) if ret != 0 else "")
        if ret != 0:
            return
        print(f"        accounts:\n{data.to_string()}")

        sim = data[data["trd_env"] == TrdEnv.SIMULATE]
        if sim.empty:
            _result("paper account available", False, "no SIMULATE in get_acc_list")
            return
        _result("paper account available", True, f"acc_id={int(sim.iloc[0]['acc_id'])}")
        sim_acc_id = int(sim.iloc[0]["acc_id"])

        _section("Trade: paper account asset")
        ret, asset = ctx.accinfo_query(trd_env=TrdEnv.SIMULATE, acc_id=sim_acc_id)
        _result("accinfo_query", ret == 0, str(asset) if ret != 0 else "")
        if ret == 0:
            print(f"        asset: {asset.iloc[0].to_dict()}")

        _section("Trade: paper positions")
        ret, pos = ctx.position_list_query(
            trd_env=TrdEnv.SIMULATE, acc_id=sim_acc_id
        )
        _result(
            "position_list_query",
            ret == 0,
            f"{len(pos)} rows" if ret == 0 else str(pos),
        )
        if ret == 0 and len(pos):
            print(f"        first row: {pos.iloc[0].to_dict()}")

        _section("Trade: today's order list")
        ret, orders = ctx.order_list_query(
            trd_env=TrdEnv.SIMULATE, acc_id=sim_acc_id
        )
        _result(
            "order_list_query",
            ret == 0,
            f"{len(orders)} rows" if ret == 0 else str(orders),
        )
    finally:
        ctx.close()


def main() -> int:
    print("Futu OpenD probe — vibe-trader")
    print("OpenD: 127.0.0.1:11111")
    try:
        probe_quote()
    except Exception as e:
        print(f"\nFATAL in quote probe: {e}")
        return 2
    try:
        probe_trade()
    except Exception as e:
        print(f"\nFATAL in trade probe: {e}")
        return 2
    print("\n=== probe complete ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
