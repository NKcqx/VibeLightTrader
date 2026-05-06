"""Render a self-dialogue decision packet (the heart of HITL).

The packet is markdown intended to be pasted into Cursor / Claude.app,
where it will be processed by another instance of the same model that
authored these instructions. We exploit that fact in three ways:

1. **Memory recall via tools.** The receiver has Read/Grep available; we
   ask it to scan the conversation transcripts and prior audit logs
   *before* deciding, so it doesn't operate from a cold start.

2. **Self-consistent constraints.** We embed the same hard constraints
   the rule + LLM strategies use, so the decision is comparable.

3. **Auditable proof of recall.** The output schema includes a
   `memory_used` field where the receiver lists what it actually read.
   If empty / boilerplate, we know to discount the decision.

A packet has two physical files:

  - `<id>.md`    the prompt (human-readable; user pastes this)
  - `<id>.json`  raw context snapshot (machine-readable; audit + replay)
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from vibe_trader.signals.base import Severity, Signal
from vibe_trader.signals.strategy_base import StrategyContext


# ---------------------------------------------------------------------------
# DecisionPacket: serialisable snapshot of everything needed to (a) render
# the prompt, (b) re-render later for audit, and (c) execute the resulting
# trade idempotently.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DecisionPacket:
    """Everything the receiver needs, plus everything we need to act on
    its reply. Written to `<id>.json` alongside the markdown prompt.
    """

    id: str
    """ULID-ish: ts + uuid4 short — sortable + collision-resistant."""

    code: str
    created_at: str  # ISO8601 UTC
    triggering_signal_ids: list[int]
    triggering_signal_types: list[str]

    snapshot: dict[str, Any] | None
    """Subset of Snapshot kept verbatim for replay. None if no live snap."""

    indicators: dict[str, float | None] | None
    """Last bar of the 60m kline: rsi_14, macd, macd_signal, macd_hist,
    boll_upper, boll_mid, boll_lower."""

    position_qty: int
    avg_cost: float
    realized_pnl: float
    intraday_return: float | None
    last_30_bar_return: float | None

    signals: list[dict[str, Any]] = field(default_factory=list)
    """Each: {signal_type, severity, payload_summary}."""

    constraints: dict[str, Any] = field(default_factory=dict)
    """max_position, min_trade_size, min_confidence, etc.
    Mirrors StrategyLLMConfig.knobs so HITL ≡ LLM in policy."""

    memory_hints: list[str] = field(default_factory=list)
    """File paths / search terms the receiver should skim for context."""


def make_packet_id(now: datetime | None = None) -> str:
    """Sortable, human-skimmable id: 20260504T103045Z_a1b2c3d4."""
    now = now or datetime.now(tz=timezone.utc)
    return f"{now:%Y%m%dT%H%M%SZ}_{uuid4().hex[:8]}"


def build_packet(
    ctx: StrategyContext,
    *,
    triggering_signal_ids: list[int],
    constraints: dict[str, Any],
    memory_hints: list[str] | None = None,
    now: datetime | None = None,
    packet_id: str | None = None,
) -> DecisionPacket:
    """Materialise a frozen packet from a `StrategyContext`.

    The caller (HITLStrategy) is responsible for resolving signal_ids
    from the persisted SignalRow ids, and providing the constraints
    block lifted from cfg.trader.strategy.hitl.
    """
    now = now or datetime.now(tz=timezone.utc)
    pid = packet_id or make_packet_id(now)

    snapshot_dict: dict[str, Any] | None = None
    if ctx.snapshot is not None:
        # Snapshot is whatever the futu client returns. Pull the fields
        # we know exist; tolerate the rest going missing.
        snapshot_dict = {
            k: getattr(ctx.snapshot, k, None)
            for k in (
                "code",
                "last_price",
                "open_price",
                "prev_close_price",
                "high_price",
                "low_price",
                "volume",
                "ts",
            )
        }
        # Datetimes don't survive json.dumps directly; stringify.
        ts = snapshot_dict.get("ts")
        if isinstance(ts, datetime):
            snapshot_dict["ts"] = ts.isoformat()

    indicators: dict[str, float | None] | None = None
    if ctx.kline_60m is not None and not ctx.kline_60m.empty:
        try:
            last = ctx.kline_60m.iloc[-1]

            def _of(name: str) -> float | None:
                v = last.get(name) if hasattr(last, "get") else None
                if v is None:
                    return None
                try:
                    f = float(v)
                except (TypeError, ValueError):
                    return None
                if f != f:  # NaN
                    return None
                return f

            indicators = {
                "rsi_14": _of("rsi_14"),
                "macd": _of("macd"),
                "macd_signal": _of("macd_signal"),
                "macd_hist": _of("macd_hist"),
                "boll_upper": _of("boll_upper"),
                "boll_mid": _of("boll_mid"),
                "boll_lower": _of("boll_lower"),
            }
        except Exception:  # pragma: no cover — never let kline shape kill packets
            indicators = None

    return DecisionPacket(
        id=pid,
        code=ctx.code,
        created_at=now.isoformat(),
        triggering_signal_ids=list(triggering_signal_ids),
        triggering_signal_types=[s.signal_type for s in ctx.signals],
        snapshot=snapshot_dict,
        indicators=indicators,
        position_qty=ctx.position_qty,
        avg_cost=ctx.avg_cost,
        realized_pnl=ctx.realized_pnl,
        intraday_return=ctx.intraday_return,
        last_30_bar_return=ctx.last_30_bar_return,
        signals=[_serialise_signal(s) for s in ctx.signals],
        constraints=dict(constraints),
        memory_hints=list(memory_hints or []),
    )


def _serialise_signal(s: Signal) -> dict[str, Any]:
    return {
        "signal_type": s.signal_type,
        "severity": (
            s.severity.value if isinstance(s.severity, Severity) else str(s.severity)
        ),
        "payload": dict(s.payload),
    }


def packet_to_json(p: DecisionPacket) -> str:
    return json.dumps(asdict(p), ensure_ascii=False, indent=2, default=str)


# ---------------------------------------------------------------------------
# Markdown rendering — the prompt itself.
# ---------------------------------------------------------------------------


# Every paragraph here is prose to the receiver; it WILL read this top-to-
# bottom. Keep imperative voice, short sentences, no jargon. The receiver
# should finish reading the packet faster than re-deriving everything.

_HEADER_TEMPLATE = """\
# 🎯 决策请求 · {code}

> **致 Claude（接收的你）**: 这条 packet 是 vibe-trader 自动生成的，
> 等你（Claude，运行在 Cursor / Claude Desktop）来做一次中长线交易决策。
> 发送方和你是同款模型，所以下面的 *self-instructions* 可以直接命令你
> 调用工具。**请按顺序完成第 1 → 第 5 步。**

**Packet ID**: `{packet_id}`
**生成时间**: {created_at}
**当前价**: {price_str}{intraday_str}

---
"""


_STEP1_TEMPLATE = """\
## 第 1 步 — 重建 MEMORY（不要跳过）

你之前跟用户讨论过 vibe-trader 的设计，下面这些上下文你**必须**召回：

- 用户画像: 中长线 3-6 个月，每标的 5W 美金，承受 ±20%，关注 NVDA / GOOG
- 系统的 4 个 α 来源: 多源信息融合 / 纪律执行 / 审计驱动迭代 / 组合一致性
- 决策原则: 调度官模式 — 规则做 trigger，LLM 做综合判断，硬约束兜底

**用以下命令实际去检索**（你有 Read / Grep 工具）:

```text
{memory_hints}
```

如果你已经在最近的 conversation 里讨论过本次决策的标的，那部分上下文应该
还在你的 MEMORY 里。如果你打开的是一个新 session，**至少**完成上面的检索
再往下做。

---
"""


_STEP2_TEMPLATE = """\
## 第 2 步 — 数据上下文（系统快照）

### 触发事件

{triggers_md}

### 实时快照
{snapshot_md}

### 技术指标（最近 60m K 线）
{indicators_md}

### 当前持仓 & 盈亏
{position_md}

### 多周期回报
{returns_md}

---
"""


_STEP3_TEMPLATE = """\
## 第 3 步 — 决策约束（必读）

| 约束 | 值 | 含义 |
|---|---|---|
| max_position | {max_position} | 单标的最大持仓股数 |
| min_trade_size | {min_trade_size} | 单笔最少交易股数 |
| min_confidence | {min_confidence} | 置信度低于此值自动降级为 HOLD |

**安全规则**:
- 模拟账户（SIMULATE），不会触及真实资金
- BUY 决策若 `position_qty + qty > max_position` 会被硬约束拦下
- SELL 决策若 `qty > position_qty` 会被硬约束拦下
- HOLD 决策的 qty 必须为 0
- 决策一旦提交将被自动执行下单（按提交时的最新市价）

---
"""


_STEP4_TEMPLATE = """\
## 第 4 步 — 输出决策 JSON

**严格按以下 schema** 输出（除此之外的解释放在 conversation 里就好，
但 JSON 块本身不能有 prose）:

```json
{
  "action": "BUY|SELL|HOLD",
  "qty": <int >= 0>,
  "confidence": <float in [0.0, 1.0]>,
  "reason": "<中文 ≤ 80 字>",
  "memory_used": [
    "<刚才你 Read/Grep 拿到的具体 file path 或 transcript 片段>",
    "..."
  ]
}
```

`memory_used` **必填且必须非空** — 这是用户分辨"你真的召回了上下文"
还是"凭空发挥"的依据。如果你确实没读到任何外部文件（比如 packet 已经
完整自包含），就明确写 `"packet 内容自包含，无需外部 MEMORY"`。

---
"""


_STEP5_TEMPLATE = """\
## 第 5 步 — 提交回 vibe-trader

把上面的 JSON 复制好，让用户跑下面这条命令把决策落实回系统:

```bash
cd /Users/bytedance/Documents/Code/vibe-trader
vibe-trader decide submit {packet_id} --json '<贴 JSON 这里>'
```

或者**你（Claude）有写文件权限**的话，直接写到:

```text
{repo_root}/var/decisions/submitted/{packet_id}.json
```

vibe-trader 会自动消费 submitted 目录里的决策，过硬约束 + 写 Trade /
Position + 推 Lark 通知用户结果。

---

> 如果你判断**当前不该交易**（数据不全、信心不足、或这不是好时机），
> 输出 action=HOLD 即可。HOLD 不是失败，是有效决策。
"""


def render_packet_md(p: DecisionPacket, *, repo_root: Path | None = None) -> str:
    """Render the packet as the markdown the user pastes into Cursor.

    `repo_root` is interpolated into the submit command and the
    suggested write-path; defaults to the current working directory
    (which is what `vibe-trader` runs in anyway).
    """
    repo_root = repo_root or Path(os.getcwd())

    price_str = "n/a"
    intraday_str = ""
    if p.snapshot:
        last = p.snapshot.get("last_price")
        if last is not None:
            try:
                price_str = f"${float(last):.2f}"
            except (TypeError, ValueError):
                price_str = str(last)
    if p.intraday_return is not None:
        sign = "▲" if p.intraday_return >= 0 else "▼"
        intraday_str = f" · 日内 {sign} {p.intraday_return:+.2%}"

    header = _HEADER_TEMPLATE.format(
        code=p.code,
        packet_id=p.id,
        created_at=p.created_at,
        price_str=price_str,
        intraday_str=intraday_str,
    )

    memory_hints = "\n".join(f"  {h}" for h in p.memory_hints) or "  (no hints)"
    step1 = _STEP1_TEMPLATE.format(memory_hints=memory_hints)

    step2 = _STEP2_TEMPLATE.format(
        triggers_md=_render_triggers(p.signals),
        snapshot_md=_render_snapshot(p.snapshot),
        indicators_md=_render_indicators(p.indicators),
        position_md=_render_position(p.position_qty, p.avg_cost, p.realized_pnl),
        returns_md=_render_returns(p.intraday_return, p.last_30_bar_return),
    )

    step3 = _STEP3_TEMPLATE.format(
        max_position=p.constraints.get("max_position", "n/a"),
        min_trade_size=p.constraints.get("min_trade_size", "n/a"),
        min_confidence=p.constraints.get("min_confidence", "n/a"),
    )

    step4 = _STEP4_TEMPLATE  # plain text
    step5 = _STEP5_TEMPLATE.format(packet_id=p.id, repo_root=str(repo_root))

    return header + step1 + step2 + step3 + step4 + step5


# ---------------------------------------------------------------------------
# Sub-renderers — each is pure, so it's trivial to unit-test edge cases.
# ---------------------------------------------------------------------------


def _render_triggers(signals: list[dict[str, Any]]) -> str:
    if not signals:
        return "(无信号)"
    lines: list[str] = []
    for s in signals:
        payload = s.get("payload") or {}
        kvs = ", ".join(f"{k}={_fmt_val(v)}" for k, v in payload.items())
        lines.append(
            f"- **{s['signal_type']}** ({s.get('severity', 'INFO')})"
            + (f" — {kvs}" if kvs else "")
        )
    return "\n".join(lines)


def _render_snapshot(snap: dict[str, Any] | None) -> str:
    if not snap:
        return "(无实时快照)"
    rows = [
        ("last_price", snap.get("last_price"), "$"),
        ("open_price", snap.get("open_price"), "$"),
        ("prev_close_price", snap.get("prev_close_price"), "$"),
        ("high_price", snap.get("high_price"), "$"),
        ("low_price", snap.get("low_price"), "$"),
        ("volume", snap.get("volume"), ""),
    ]
    out: list[str] = []
    for label, val, prefix in rows:
        if val is None:
            continue
        out.append(f"- `{label}`: {prefix}{_fmt_val(val)}")
    if "ts" in snap and snap["ts"]:
        out.append(f"- `ts`: {snap['ts']}")
    return "\n".join(out) if out else "(快照字段全空)"


def _render_indicators(ind: dict[str, float | None] | None) -> str:
    if not ind:
        return "(无指标 — kline 不可用)"
    rows = [
        ("RSI(14)", ind.get("rsi_14"), 2),
        ("MACD", ind.get("macd"), 4),
        ("MACD signal", ind.get("macd_signal"), 4),
        ("MACD hist", ind.get("macd_hist"), 4),
        ("Bollinger upper", ind.get("boll_upper"), 2),
        ("Bollinger mid", ind.get("boll_mid"), 2),
        ("Bollinger lower", ind.get("boll_lower"), 2),
    ]
    out: list[str] = []
    for label, v, decimals in rows:
        if v is None:
            out.append(f"- `{label}`: n/a")
        else:
            out.append(f"- `{label}`: {v:.{decimals}f}")
    return "\n".join(out)


def _render_position(qty: int, avg_cost: float, realized_pnl: float) -> str:
    return (
        f"- 持仓: **{qty}** 股 @ 均价 ${avg_cost:.2f}\n"
        f"- 已实现盈亏: ${realized_pnl:+.2f}"
    )


def _render_returns(intraday: float | None, last_30: float | None) -> str:
    parts: list[str] = []
    if intraday is not None:
        sign = "▲" if intraday >= 0 else "▼"
        parts.append(f"- 日内: {sign} {intraday:+.2%}")
    else:
        parts.append("- 日内: n/a")
    if last_30 is not None:
        sign = "▲" if last_30 >= 0 else "▼"
        parts.append(f"- 近 30 根 (60m): {sign} {last_30:+.2%}")
    else:
        parts.append("- 近 30 根: n/a")
    return "\n".join(parts)


def _fmt_val(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:.4f}".rstrip("0").rstrip(".") or "0"
    return str(v)


# ---------------------------------------------------------------------------
# Default memory hints — what the receiver should grep/read first. Caller
# can override or extend, but this list is a safe default.
# ---------------------------------------------------------------------------


def default_memory_hints(repo_root: Path | None = None, code: str = "") -> list[str]:
    """Suggested Read/Grep commands the receiver should run first.

    Phrased as terminal commands so the receiver can literally execute
    them via the Shell tool. Tuned for the vibe-trader repo layout.
    """
    repo = str(repo_root) if repo_root else "/Users/bytedance/Documents/Code/vibe-trader"
    transcripts = "/Users/bytedance/.cursor/projects/Users-bytedance-Documents-Code/agent-transcripts/"
    audit = f"{repo}/data/llm_decisions.jsonl"
    db = f"{repo}/data/vibe_trader.db"

    hints = [
        f"# 1) 召回我们讨论过的 4 个 α 来源 + 决策框架",
        f'rg -n "α 来源|调度官|调度|HITL" {transcripts} | head -40',
        f"",
        f"# 2) 读 README 第 \"自动交易策略\" / \"风控\" 节",
        f"sed -n '/^## /p' {repo}/README.md   # 看目录",
        f"",
        f"# 3) 之前你（往届的同款 Claude）做过的决策（如有）",
        f"tail -n 50 {audit} 2>/dev/null | jq -c '{{ts:.ts_unix, code, decision}}'",
        f"",
        f"# 4) 这只标的的最近持仓 + 已平仓历史",
    ]
    if code:
        hints += [
            f"sqlite3 {db} \"SELECT ts, side, qty, price, status FROM trades t "
            f"JOIN symbols s ON s.id=t.symbol_id WHERE s.code='{code}' "
            f"ORDER BY ts DESC LIMIT 10;\"",
            f"",
        ]
    hints += [
        f"# 5) 当前 signal_alert 卡片的释义函数（确保你跟 Lark 卡片解读一致）",
        f"rg -n 'def explain_signal' {repo}/src/vibe_trader/reports/render.py",
    ]
    return hints
