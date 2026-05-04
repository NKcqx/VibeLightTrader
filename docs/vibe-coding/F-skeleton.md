# Part F · 可复刻的最小骨架

> 你不需要复刻这个项目的代码。你需要复刻**它的骨架**，然后让你的 AI 跟着 Part C 的剧本帮你填肉。
>
> 本节给你 4 样东西：① 项目目录骨架 ② 三个核心 Protocol ③ settings.yaml 骨架 ④ 30 行 main loop demo（直接能跑）。

---

## 1. 项目目录骨架

```
your-project/
├── README.md
├── pyproject.toml                # 或 requirements.txt
├── config/
│   ├── settings.yaml             # 全局配置（数据源 / 调度 / LLM / 通知 / 投资者画像）
│   └── watchlist.yaml            # 标的 + 阈值
├── src/
│   └── your_project/
│       ├── __init__.py
│       ├── cli/
│       │   └── main.py           # CLI 入口（click / typer）
│       ├── data/                 # 数据获取（broker / 数据源 / 指标计算）
│       │   ├── quotes.py
│       │   └── indicators.py
│       ├── signals/              # 信号检测 + 策略
│       │   ├── base.py           # Signal 数据类
│       │   ├── threshold.py      # 阈值检测
│       │   ├── tech.py           # RSI/MACD/Boll 检测
│       │   ├── strategy_base.py  # ★ Strategy Protocol
│       │   ├── strategy_rule.py  # 规则策略
│       │   └── strategy_llm.py   # LLM 策略
│       ├── llm/                  # LLM 客户端 + prompt 渲染
│       │   ├── client.py         # ★ LLMClient Protocol
│       │   ├── prompt.py
│       │   └── factory.py        # 根据 provider 返回 client 实例
│       ├── trader/               # 模拟交易接口
│       │   ├── paper.py          # ★ PaperTrader Protocol
│       │   └── execute.py
│       ├── reports/              # 通知（飞书 / 邮件 / Slack）
│       ├── scheduler/            # APScheduler 调度
│       ├── journal/              # 决策审计 + 每标的 markdown journal
│       ├── decisions/            # 决策包（HITL）
│       └── models.py             # SQLAlchemy 数据库模型
├── tests/
│   ├── unit/
│   └── conftest.py
├── data/                         # 运行时产物（DB / 日志 / journal markdown）
│   ├── your_project.db
│   ├── llm_decisions.jsonl
│   ├── journal/
│   └── dev_log.md
└── docs/
```

**骨架精神**：

- **层次分明** —— `data/` 拉数据，`signals/` 出结论，`trader/` 下单，`reports/` 通知。每层只做一件事
- **接口先行** —— 三个 ★ 文件是 Protocol（接口），其他文件是实现。**Protocol 写完之前不写实现**
- **数据库与运行时产物在 `data/`** —— git ignore 它，不要污染代码区

---

## 2. 三个核心 Protocol（这是灵魂）

### 2.1 Strategy Protocol（策略层）

```python
# src/your_project/signals/strategy_base.py
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

# Signal 是上游信号检测的产出（threshold breach / RSI overbought 等）
@dataclass(frozen=True)
class Signal:
    code: str
    kind: str            # e.g. "threshold_breach_upper", "rsi_overbought"
    severity: Literal["info", "warn", "critical"]
    payload: dict[str, Any]

@dataclass(frozen=True)
class StrategyContext:
    """Strategy.decide 的唯一参数。加新数据点不用改 Protocol。"""
    code: str
    signals: list[Signal]
    position_qty: int = 0
    avg_cost: float = 0.0
    # 加新字段时设默认值 = 老 strategy 不用动
    snapshot: Any | None = None
    kline_60m: Any | None = None
    config: dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class SignalSuggest:
    """策略输出：'我想做什么 + 多大量 + 为什么 + 多自信'"""
    code: str
    action: Literal["BUY", "SELL", "HOLD"]
    qty: int
    reason: str
    severity: Literal["info", "warn", "critical"]
    confidence: float | None = None
    raw_llm_text: str | None = None
    latency_ms: int | None = None

@runtime_checkable
class Strategy(Protocol):
    name: str   # 'rule' / 'llm' / 'hitl' / 'ensemble:...'
    def decide(self, ctx: StrategyContext) -> SignalSuggest | None: ...

# 简易 registry
_REGISTRY: dict[str, Any] = {}
def register_strategy(name: str):
    def deco(builder):
        _REGISTRY[name] = builder
        return builder
    return deco
def build_strategy(name: str, config: dict) -> Strategy:
    return _REGISTRY[name](config or {})
```

**为什么这么设计**：

- 加一种新策略 = 写一个 `class XxxStrategy:` + `@register_strategy("xxx")` 装饰器，**其他模块不用动**
- `StrategyContext` 字段加新的不破坏老策略——老策略读不到的字段就是 `None`
- 返回 `None` = 沉默；返回 `HOLD` = 显式表态。这两种**写入审计日志时不一样**

### 2.2 PaperTrader Protocol（交易层）

```python
# src/your_project/trader/paper.py
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

OrderSide = Literal["BUY", "SELL"]
OrderType = Literal["MARKET", "LIMIT"]
OrderStatus = Literal["FILLED", "PENDING", "REJECTED", "CANCELLED"]

@dataclass(frozen=True)
class PaperOrderResult:
    order_id: str
    status: OrderStatus
    code: str
    side: OrderSide
    requested_qty: int
    filled_qty: int
    avg_fill_price: float
    submitted_at: datetime
    error: str | None = None

@dataclass(frozen=True)
class PaperPosition:
    code: str
    qty: int
    avg_cost: float
    market_value: float | None = None
    unrealized_pnl: float | None = None

@runtime_checkable
class PaperTrader(Protocol):
    def place_order(self, *, code: str, side: OrderSide, qty: int,
                    order_type: OrderType = "MARKET",
                    limit_price: float | None = None) -> PaperOrderResult: ...
    def query_positions(self) -> list[PaperPosition]: ...
    def query_today_orders(self) -> list[Any]: ...
    def close(self) -> None: ...
```

**两个实现**：

- `FakePaperTrader`：内存版，所有单元测试都用它（确定性、零依赖）
- `OpenDSecTrader`（或你的 broker 等价物）：接真实 broker 模拟账户

**关键决策**：让一切**测试都用 Fake，生产都用 Real**——这道边界就是这个 Protocol。

### 2.3 LLMClient Protocol（LLM 层）

```python
# src/your_project/llm/client.py
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TypedDict, runtime_checkable

class Message(TypedDict):
    role: Literal["system", "user", "assistant"]
    content: str

@dataclass(frozen=True)
class LLMResponse:
    text: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    finish_reason: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

# 错误层级 ——  策略层根据 fallback_on_error 决定降级到 rule 还是 hold
class LLMError(Exception):
    def __init__(self, msg: str, *, provider: str | None = None) -> None:
        super().__init__(msg); self.provider = provider
class LLMTimeoutError(LLMError): ...
class LLMHTTPError(LLMError): ...
class LLMAuthError(LLMHTTPError): ...      # 401/403 不重试
class LLMRateLimitError(LLMHTTPError): ... # 429 可退避重试
class LLMParseError(LLMError): ...         # JSON 解析失败

@runtime_checkable
class LLMClient(Protocol):
    name: str   # "cursor-agent:default" / "anthropic:claude-3-5-sonnet"
    model: str
    def chat(self, messages: list[Message], *,
             max_tokens: int, temperature: float, timeout_s: float) -> LLMResponse: ...
```

**为什么**：让你的项目能在 cursor-agent / Anthropic SDK / OpenAI / DeepSeek / OpenRouter / Ollama 之间切换 = **一个 yaml 字段**的事。

**这是 vibe coder 杠杆最大的抽象之一**——后面你想换 LLM 试效果，5 分钟搞定。

---

## 3. settings.yaml 骨架（带注释）

```yaml
# ----- 数据源 -----
opend:
  host: 127.0.0.1
  port: 11111

# ----- 数据库 -----
database:
  path: data/your_project.db

# ----- 调度（cron 是 NY 时间，按你的市场调）-----
scheduler:
  timezone: America/New_York
  jobs:
    intraday_check: { cron: "30 9-15 * * mon-fri" }   # 每小时一次
    morning_brief:  { cron: "30 10 * * mon-fri" }
    closing_brief:  { cron: "30 16 * * mon-fri" }

# ----- 通知 -----
lark:
  cli_path: lark-cli
  identity: bot                    # bot 不需要额外 scope
  receiver:
    type: user                     # user / chat
    open_id: "ou_xxxxxxxx"         # 你的飞书 open_id

# ----- 信号阈值 -----
signals:
  rsi_overbought: 70
  rsi_oversold: 30
  bollinger_period: 20
  bollinger_std: 2
  macd_fast: 12
  macd_slow: 26
  macd_signal: 9

# ----- 交易（核心）-----
trader:
  auto_execute: true               # true = 自动下模拟单
  simulate_only: true              # ★ 安全锁：只允许 SIMULATE 账户

  strategy:
    type: llm                      # rule | llm | hitl | ensemble

    rule:
      max_position_per_symbol: 200
      critical_size: 100
      warn_size: 50

    llm:
      provider: cursor-agent       # cursor-agent | anthropic | openai_compat
      model: ""                    # cursor-agent: ""=默认；anthropic 填模型名
      timeout_s: 240
      max_position_per_symbol: 200
      min_trade_size: 10
      min_confidence: 0.6
      fallback_on_error: rule      # rule | hold
      audit_log_path: data/llm_decisions.jsonl
      kline_window: 200
      cache_seconds: 300

  # ★ 投资者画像：决策上下文，独立于策略。
  # 详见 docs/mid-term-investing.md（保守 / 平衡 / 进取 套餐）
  investment_profile:
    enabled: true
    horizon_months_min: 3
    horizon_months_max: 6
    style: growth
    theme: "AI-infrastructure & cloud-incumbent mid-term swing"
    budget_per_symbol_usd: 50000
    drawdown_tolerance_pct: 20
    max_concentration_pct: 60
    initial_entry_pct: 40
    max_batches: 3
    add_on_dip_pct: 5
    add_cooldown_days: 5
    take_profit_pct: 30
    take_profit_trim_pct: 50
    hard_stop_pct: 20
    min_holding_days: 30
```

**配置精神**：

- **每段独立、可替换** —— 换 broker、换 LLM、换通知都是改一段
- **dormant section 保留** —— 不用的 strategy（hitl / ensemble）也写默认值，方便切换
- **comment 解释每个字段** —— 给你的 AI 看，让它别瞎填

---

## 4. 30 行 main loop demo（最小可运行版）

下面这段代码**不依赖项目任何模块**——你可以**直接给你的 AI**作为起点：

```python
"""minimum-viable equity monitor: pull, signal, decide, notify.

单文件版，跑得通；后面让 AI 帮你抽 Protocol、拆模块、加 DB / 飞书 / LLM。

Day 0：用 print 替代飞书、用硬编码价替代 OpenD；先验证逻辑闭环。
Day 1：把 fetch_quote 接到真实 broker；
Day 2：把 notify 接到飞书；
Day 5：把 decide 抽出来变成 Strategy Protocol，rule 实现保留，llm 实现新增。
"""
from dataclasses import dataclass
import time

@dataclass
class Quote:
    code: str
    last: float

def fetch_quote(code: str) -> Quote:
    # TODO Day 1：换成你的数据源（富途 OpenD / Yahoo / IBKR）
    fake_prices = {"US.NVDA": 198.45, "US.MSFT": 412.30}
    return Quote(code=code, last=fake_prices.get(code, 0.0))

def detect_signal(q: Quote, *, upper: float, lower: float) -> str | None:
    if q.last >= upper:
        return "threshold_breach_upper"
    if q.last <= lower:
        return "threshold_breach_lower"
    return None

def decide(signal: str, position_qty: int) -> tuple[str, int]:
    # 最简陋的规则；Day 5 替成 Strategy Protocol
    if signal == "threshold_breach_upper" and position_qty > 0:
        return "SELL", position_qty
    if signal == "threshold_breach_lower":
        return "BUY", 50
    return "HOLD", 0

def notify(msg: str) -> None:
    # TODO Day 2：换成飞书 lark-cli send-text
    print(f"[notify] {msg}")

WATCHLIST = [("US.NVDA", 220.0, 170.0), ("US.MSFT", 480.0, 360.0)]
positions: dict[str, int] = {"US.NVDA": 0, "US.MSFT": 0}

def tick() -> None:
    for code, upper, lower in WATCHLIST:
        q = fetch_quote(code)
        sig = detect_signal(q, upper=upper, lower=lower)
        if sig:
            action, qty = decide(sig, positions.get(code, 0))
            notify(f"{code} last={q.last} signal={sig} -> {action} {qty}")

if __name__ == "__main__":
    while True:
        tick()
        time.sleep(60 * 60)   # 每小时一次
```

**验证它工作**：

```bash
python minimum_viable.py
# 输出：
# [notify] US.NVDA last=198.45 signal=None -> ...   <-- 不会触发
# 修改 fake_prices 让 NVDA = 230，重跑：
# [notify] US.NVDA last=230 signal=threshold_breach_upper -> HOLD 0
```

**这是项目的 Day 0**：跑通它，反馈环拿到。下一步把它跟 AI 一起，按 [Part C](./C-evolution-timeline.md) 的剧本逐天扩展。

---

## 5. 接下来 7 步：跟 AI 一起填肉

按 Part C 的 7 天剧本（**下批次写**，先看大纲），每天给 AI 一个具体目标：

| Day | 跟 AI 说 |
|---|---|
| 1 | "把 `fetch_quote` 接到富途 OpenD；行情存 SQLite `quotes` 表" |
| 2 | "加 RSI/MACD/Boll 计算 + K 线图导出 PNG + 飞书发送" |
| 3 | "把 `decide` 抽成 `Strategy` Protocol；保留 rule 实现 + `FakePaperTrader` 单测覆盖" |
| 4 | "把 `notify` 改成飞书卡片；引入 `OpenDSecTrader` 接真实模拟账户" |
| 5 | "加 `LLMStrategy`：cursor-agent 优先，fallback 到 rule；prompt 用 Jinja2" |
| 6 | "加决策审计日志 + 每标的 Markdown journal" |
| 7 | "加投资者画像 `investment_profile` + 主动 `analyze` CLI" |

**每一步都先 ask plan，再动手，再 commit**（参见 [Part B](./B-meta-skills.md) Skill 1）。

---

## 6. Day 0 checklist（开干前）

- [ ] 我已经把骨架目录建好了
- [ ] 三个 Protocol 文件已经写好（哪怕只是空 stub）
- [ ] `settings.yaml` 已经填了**我自己的真实参数**（开盘时段 / 标的 / 投资者画像）
- [ ] 30 行 main loop 我**手动跑通过一次**（用 fake 数据）
- [ ] 我把 [Part A](./A-kickoff.md) 的"开局 prompt"贴给我的 AI 看过

打满 5 个勾，开始 Day 1。

---

## 下一步

- 👉 想看 7 天怎么具体推进 → [Part C · 演进时间线](./C-evolution-timeline.md)（下批次写）
- 👉 卡住了 → [Part B · 7 个 meta-skill](./B-meta-skills.md)
- 👉 有些事一直做不出来 → [Part E · AI 写不出来的清单](./E-ai-cant-do-this.md)（下批次写）
