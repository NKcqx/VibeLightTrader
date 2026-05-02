# Equity Monitor — Design Spec

**Date:** 2026-05-02
**Status:** Approved (pending user review)
**Topic:** 美股标的实时监控、技术面分析、舆情聚合与模拟操盘系统

---

## 1. 背景与目标

打造一个**长驻在本地 Mac 的 Python 服务**，针对一组用户配置的美股标的：

- **每小时**抓取行情、技术指标（RSI / MACD / BOLL）、Futu 技术异动信号、资金异动信号、新闻摘要、评论情绪
- 按预设规则生成结构化交易信号
- 通过**飞书消息**推送告警与盘点报告
- 把全量历史落地到 SQLite 便于回溯
- 后续阶段在此基础上叠加"半自动建议下单 → 全自动模拟交易 → 自动复盘日报"

### 三阶段交付边界

| 阶段 | 范围 | Deliverable | 验收标准 |
|---|---|---|---|
| **Phase 1 决策助手** | 数据采集 + 信号生成 + 飞书推送 | `equity-monitor run` 长驻；混合触发（信号告警 + 开盘1h/收盘盘点） | 飞书每收到一条卡片，涵盖：当前价、当日涨跌、RSI/MACD/BOLL 状态、Futu 技术异动、资金异动、新闻情绪、价格阈值告警 |
| **Phase 2 半自动** | 卡片附带"建议动作" + paper trading 下单 + 用户 confirm | 卡片附 `Suggest: BUY 100 AAPL @ 175.5`；用户在飞书 reply `confirm <signal_id>` 触发 Futu Paper Trading 下单 | SQLite `trades` 表落账；下次盘点卡片显示模拟仓位 P&L |
| **Phase 3 全自动** | 策略基线 + 风控 + 自动下单 + 日报 | 策略接收 Signal → 仓位/资金管理 → 自动调 Futu paper API；每日 ET 16:30 出复盘日报 | 日报含：今日交易、当前仓位、累计 P&L、回撤、胜率 |

**关键原则：Phase 1 必须独立成立。Phase 2/3 是"加层"，不重写核心。**

本 spec 详细覆盖 Phase 1，Phase 2/3 的接口预留与扩展点会显式标注，但具体实现细节留给后续 spec。

---

## 2. 范围与非范围

### 范围（Phase 1）

- 美股标的（NYSE / NASDAQ）
- YAML 文件管理监控列表与阈值
- Futu OpenD 作为唯一行情/异动数据源
- 飞书消息为唯一推送通道（subprocess 调 lark-cli）
- SQLite 单文件存储

### 非范围（明确不做）

- 港股 / A 股 / 加密货币
- 机构评级、分析师 target price 等基本面（后续阶段补 yfinance）
- 真实资金交易（始终走 Futu Paper Trading SIMULATE 环境）
- Web UI / Dashboard（盘点直接进飞书消息卡片）
- 多用户 / 多账号（单用户单 Futu 账号）

---

## 3. 系统架构

```
                         ┌────────────────────────────────────────┐
                         │  config/watchlist.yaml + settings.yaml │
                         └────────────────────────────────────────┘
                                          │
                                          ▼
┌────────────┐   long-running APScheduler  ┌──────────────────────────────┐
│  CLI       │──────────────────────────► │   scheduler.runner (entry)   │
│  run/once  │                             └──────────────────────────────┘
│  backfill  │                                  │
└────────────┘                                  ▼
                          ┌─────────── Data Layer ───────────┐
                          │  futu_client (OpenD socket)       │
                          │  data.quotes / data.kline         │
                          │  data.tech_anomaly                │── Futu Anomaly Skill
                          │  data.capital_anomaly             │── Futu Anomaly Skill
                          │  data.news / data.sentiment       │── Futu Search Skill
                          └────────────┬─────────────────────┘
                                       ▼
                          ┌─────────── Persistence ──────────┐
                          │  SQLite + SQLAlchemy 2.x          │
                          │  Quote / Indicator / Signal /     │
                          │  NewsDigest / Trade / Position    │
                          └────────────┬─────────────────────┘
                                       ▼
                          ┌─────────── Signal Engine ────────┐
                          │  signals.threshold (价格阈值)      │
                          │  signals.tech (RSI/MACD/BOLL)     │
                          │  signals.compose (合成 + 去重)     │
                          └────────────┬─────────────────────┘
                                       ▼
                          ┌─────────── Reporting ────────────┐
                          │  reports.render → Lark Card JSON  │
                          │  reports.lark → lark-cli 推送     │
                          └──────────────────────────────────┘
```

### 模块责任

| 模块 | 责任 | 关键依赖 |
|---|---|---|
| `config.py` | pydantic v2 加载 YAML，强类型校验 | `pyyaml`, `pydantic` |
| `db.py` | SQLAlchemy session 工厂、WAL 模式开启、Alembic 迁移入口 | `sqlalchemy`, `alembic` |
| `models.py` | 7 张 ORM 表（Phase 1 用 5 张） | `sqlalchemy` |
| `futu_client.py` | 单例 `OpenQuoteContext`，断连重试，提供 Protocol 便于测试 mock | `futu-api`, `tenacity` |
| `data/quotes.py` | 实时报价 → `quotes` 表 | futu_client |
| `data/kline.py` | 历史 K 线（小时/日级） → 内存供 indicators 计算 | futu_client |
| `data/indicators.py` | RSI(Wilder)/MACD/Bollinger 自行实现 → `indicators` 表 | `pandas` |
| `data/tech_anomaly.py` | 调 Futu Technical Anomaly skill，解析事件 | subprocess |
| `data/capital_anomaly.py` | 调 Futu Capital Anomaly skill | subprocess |
| `data/news.py` | 调 Futu News Search / Stock Digest | subprocess |
| `data/sentiment.py` | 调 Futu Comment Sentiment | subprocess |
| `signals/base.py` | `Signal` dataclass + `Severity` enum | (无) |
| `signals/threshold.py` | 价格穿越阈值检测 | models |
| `signals/tech.py` | RSI/MACD/BOLL 信号 | models |
| `signals/compose.py` | 多源信号合成、去重、严重度判定 | (无) |
| `reports/card.py` | 飞书 Interactive Card JSON Schema | (无) |
| `reports/render.py` | jinja2 模板渲染 Signal/Position → Card | `jinja2` |
| `reports/lark.py` | subprocess `lark im +send-card` 推送 | (无) |
| `scheduler/calendar.py` | 美股交易日 + DST | `pandas-market-calendars` |
| `scheduler/jobs.py` | `intraday_check` / `morning_brief` / `closing_brief` / `news_pulse` | 上面所有层 |
| `scheduler/runner.py` | APScheduler BlockingScheduler 长驻入口 | `apscheduler` |
| `cli/main.py` | `equity-monitor run / once / backfill / watchlist` | `click` |

**单一职责原则**：每个 `data/*.py` 文件对应一个数据源；每个 `signals/*.py` 对应一类信号。新增数据源或信号不动旧代码。

---

## 4. 项目布局

```
equity-monitor/
├── pyproject.toml
├── README.md
├── alembic.ini
├── alembic/
│   ├── env.py
│   └── versions/
├── config/
│   ├── watchlist.example.yaml      # 示例配置
│   ├── watchlist.yaml              # 实际配置（gitignore）
│   └── settings.yaml
├── src/equity_monitor/
│   ├── __init__.py
│   ├── config.py
│   ├── db.py
│   ├── models.py
│   ├── futu_client.py
│   ├── data/
│   │   ├── __init__.py
│   │   ├── quotes.py
│   │   ├── kline.py
│   │   ├── indicators.py
│   │   ├── tech_anomaly.py
│   │   ├── capital_anomaly.py
│   │   ├── news.py
│   │   └── sentiment.py
│   ├── signals/
│   │   ├── __init__.py
│   │   ├── base.py
│   │   ├── threshold.py
│   │   ├── tech.py
│   │   └── compose.py
│   ├── reports/
│   │   ├── __init__.py
│   │   ├── card.py
│   │   ├── render.py
│   │   └── lark.py
│   ├── scheduler/
│   │   ├── __init__.py
│   │   ├── calendar.py
│   │   ├── jobs.py
│   │   └── runner.py
│   └── cli/
│       ├── __init__.py
│       └── main.py
├── tests/
│   ├── __init__.py
│   ├── conftest.py                 # FakeFutuClient, in-memory SQLite fixture
│   ├── unit/
│   │   ├── test_config.py
│   │   ├── test_indicators.py
│   │   ├── test_signals_threshold.py
│   │   ├── test_signals_tech.py
│   │   ├── test_compose.py
│   │   ├── test_card_render.py
│   │   └── test_calendar.py
│   └── integration/
│       └── test_scheduler_smoke.py
├── scripts/
│   └── install_opend.sh            # 引导用户走 /install-futu-opend
└── data/
    └── equity_monitor.db           # SQLite (gitignore)
```

---

## 5. 配置（YAML）

### `config/watchlist.yaml`

```yaml
symbols:
  - code: US.AAPL
    name: Apple
    upper_threshold: 200.0      # 突破推 CRITICAL
    lower_threshold: 165.0      # 跌破推 CRITICAL（用作止损线）
    notes: "core position"

  - code: US.NVDA
    name: NVIDIA
    upper_threshold: 150.0
    lower_threshold: 110.0

  - code: US.TSLA
    name: Tesla
    # 阈值可省略 → 仅靠技术信号触发
```

### `config/settings.yaml`

```yaml
opend:
  host: 127.0.0.1
  port: 11111

database:
  path: data/equity_monitor.db
  wal_mode: true

scheduler:
  timezone: America/New_York
  jobs:
    intraday_check:
      cron: "30 9-15 * * mon-fri"   # 09:30, 10:30, ..., 15:30 ET
    morning_brief:
      cron: "30 10 * * mon-fri"     # 开盘后 1h
    closing_brief:
      cron: "30 16 * * mon-fri"     # 收盘后 30 分钟
    news_pulse:
      cron: "*/30 9-15 * * mon-fri"

lark:
  cli_path: lark-cli                # 假设在 PATH
  receiver:
    type: chat                       # chat | user
    open_id: "ou_xxxxxxxxxxxxxxxx"   # 私聊给自己

signals:
  rsi_overbought: 70
  rsi_oversold: 30
  bollinger_period: 20
  bollinger_std: 2
  macd_fast: 12
  macd_slow: 26
  macd_signal: 9
  dedupe_window_minutes: 60          # 同标的同信号去重窗口
  news_burst_drop: 3.0               # 情绪温度 1h 内下跌 ≥ 3 触发负面 burst
  news_burst_rise: 3.0

logging:
  level: INFO
  file: data/equity_monitor.log
```

---

## 6. SQLite Schema

```sql
-- 标的元数据（启动时与 watchlist.yaml 同步）
CREATE TABLE symbols (
  id INTEGER PRIMARY KEY,
  code TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  market TEXT NOT NULL DEFAULT 'US',
  currency TEXT NOT NULL DEFAULT 'USD',
  lot_size INTEGER NOT NULL DEFAULT 1,
  upper_threshold REAL,
  lower_threshold REAL,
  notes TEXT,
  is_active BOOLEAN NOT NULL DEFAULT 1,
  created_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP NOT NULL
);

-- 时序行情快照（每小时一条）
CREATE TABLE quotes (
  id INTEGER PRIMARY KEY,
  symbol_id INTEGER NOT NULL REFERENCES symbols(id),
  ts TIMESTAMP NOT NULL,                -- UTC
  open REAL NOT NULL,
  high REAL NOT NULL,
  low  REAL NOT NULL,
  close REAL NOT NULL,
  volume INTEGER NOT NULL,
  turnover REAL NOT NULL,
  UNIQUE(symbol_id, ts)
);
CREATE INDEX idx_quotes_symbol_ts ON quotes(symbol_id, ts DESC);

-- 计算后的指标（每小时一条）
CREATE TABLE indicators (
  id INTEGER PRIMARY KEY,
  symbol_id INTEGER NOT NULL REFERENCES symbols(id),
  ts TIMESTAMP NOT NULL,
  rsi_14 REAL,
  macd REAL,
  macd_signal REAL,
  macd_hist REAL,
  boll_upper REAL,
  boll_mid REAL,
  boll_lower REAL,
  UNIQUE(symbol_id, ts)
);
CREATE INDEX idx_indicators_symbol_ts ON indicators(symbol_id, ts DESC);

-- 信号事件
CREATE TABLE signals (
  id INTEGER PRIMARY KEY,
  symbol_id INTEGER NOT NULL REFERENCES symbols(id),
  ts TIMESTAMP NOT NULL,
  signal_type TEXT NOT NULL,             -- 'rsi_overbought' / 'macd_golden_cross' ...
  severity TEXT NOT NULL,                -- 'INFO' | 'WARN' | 'CRITICAL'
  payload_json TEXT NOT NULL,            -- JSON: 触发值、阈值、相关新闻 url 等
  delivered BOOLEAN NOT NULL DEFAULT 0,
  delivery_ts TIMESTAMP,
  delivery_msg_id TEXT,
  UNIQUE(symbol_id, ts, signal_type)
);
CREATE INDEX idx_signals_symbol_ts ON signals(symbol_id, ts DESC);
CREATE INDEX idx_signals_undelivered ON signals(delivered) WHERE delivered = 0;

-- 新闻 + 情绪摘要
CREATE TABLE news_digest (
  id INTEGER PRIMARY KEY,
  symbol_id INTEGER NOT NULL REFERENCES symbols(id),
  ts TIMESTAMP NOT NULL,
  source TEXT,
  title TEXT NOT NULL,
  url TEXT NOT NULL,
  summary TEXT,
  sentiment_score REAL,                  -- -1.0 到 +1.0
  UNIQUE(symbol_id, url)
);
CREATE INDEX idx_news_symbol_ts ON news_digest(symbol_id, ts DESC);

-- Phase 2/3 启用
CREATE TABLE trades (
  id INTEGER PRIMARY KEY,
  symbol_id INTEGER NOT NULL REFERENCES symbols(id),
  ts TIMESTAMP NOT NULL,
  side TEXT NOT NULL,                    -- 'BUY' | 'SELL'
  qty INTEGER NOT NULL,
  price REAL NOT NULL,
  futu_order_id TEXT,
  signal_id INTEGER REFERENCES signals(id),
  status TEXT NOT NULL                   -- 'PENDING' | 'FILLED' | 'CANCELLED' | 'FAILED'
);

CREATE TABLE positions (
  id INTEGER PRIMARY KEY,
  symbol_id INTEGER NOT NULL UNIQUE REFERENCES symbols(id),
  qty INTEGER NOT NULL DEFAULT 0,
  avg_cost REAL NOT NULL DEFAULT 0,
  unrealized_pnl REAL NOT NULL DEFAULT 0,
  realized_pnl REAL NOT NULL DEFAULT 0,
  updated_at TIMESTAMP NOT NULL
);
```

**SQLite 调优**：启动时打开 WAL：`PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;`。

---

## 7. 调度时序

时区基准：**America/New_York**（自动 DST），所有时间戳以 UTC 存库，渲染时转 ET 与 Asia/Shanghai 双显示。

| Job | Cron (ET) | 行为 | 推送条件 |
|---|---|---|---|
| `intraday_check` | `30 9-15 * * mon-fri` | 拉行情 / 算指标 / 跑信号引擎 | **仅在有信号**（`WARN+`）时推送一条聚合卡片 |
| `morning_brief` | `30 10 * * mon-fri` | 开盘后 1h 全量盘点 | 必推 |
| `closing_brief` | `30 16 * * mon-fri` | 收盘后 30 分钟全量盘点 | 必推 |
| `news_pulse` | `*/30 9-15 * * mon-fri` | 刷新新闻 + 评论情绪 | 仅在重大舆情 burst 时推一条精简告警 |

**节假日处理**：`scheduler/calendar.py` 用 `pandas_market_calendars.get_calendar("NYSE")`，判断当天 `is_session`；早收盘日（如 Black Friday、Christmas Eve）截断 cron 范围；非交易日所有 job skip。

**幂等性**：每个 job 入口查 `signals` 表里"上一次同类型成功执行 ts"，若 < 30 分钟前已成功则 skip 当次（处理重启）。

**重试**：`@tenacity.retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))` 装饰所有 `data/*.py` 调用。

---

## 8. 信号合成

### 8.1 原子信号清单

| signal_type | 触发条件 | 默认 severity |
|---|---|---|
| `threshold_breach_upper` | close ≥ symbols.upper_threshold | CRITICAL |
| `threshold_breach_lower` | close ≤ symbols.lower_threshold | CRITICAL |
| `rsi_overbought` | RSI(14) > 70 | WARN |
| `rsi_oversold` | RSI(14) < 30 | WARN |
| `macd_golden_cross` | macd_hist 由 ≤0 转 >0 | WARN |
| `macd_death_cross` | macd_hist 由 ≥0 转 <0 | WARN |
| `boll_upper_break` | close > boll_upper | INFO |
| `boll_lower_break` | close < boll_lower | INFO |
| `futu_tech_anomaly` | Futu Anomaly skill 返回任意技术异动 | WARN（反转形态升级 CRITICAL） |
| `futu_capital_anomaly` | Futu Anomaly skill 返回主力净流入/出 \| 大单集中 \| 空头突增 | WARN |
| `news_negative_burst` | 1h 内 sentiment 下跌 ≥ 3.0 (10 分制) **且** 新闻含负面关键词 | CRITICAL |
| `news_positive_burst` | 1h 内 sentiment 上涨 ≥ 3.0 | WARN |

### 8.2 合成规则（`signals/compose.py`）

1. **去重**：同 (symbol_id, signal_type, hour) 在 `dedupe_window_minutes` 内只保留首次发生
2. **聚合推送**：
   - **CRITICAL** → 立即单条推送
   - **WARN** → 在 `intraday_check` 末尾合并到一条卡片（按 symbol 分组）
   - **INFO** → 仅落库，盘点卡片汇总展示
3. **每条 signal 写入 SQLite 时 `delivered=0`，推送成功后写 `delivered=1, delivery_ts, delivery_msg_id`**

### 8.3 信号 → Card 的映射

| Card 类型 | 触发 | 包含信号 |
|---|---|---|
| Signal Alert Card | CRITICAL 即时 / WARN 小时聚合 | 当次 hour 的 WARN+CRITICAL |
| Daily Brief Card | morning_brief / closing_brief | 全部 INFO+WARN+CRITICAL，按 symbol 分组 |
| News Pulse Card | news_pulse 检测到 burst | `news_negative_burst` / `news_positive_burst` |

---

## 9. 飞书消息卡片

### 9.1 三种 Card Template

详细字段见上文 §3 设计章节。卡片用飞书 Interactive Card 协议（JSON Schema），用 jinja2 模板生成。

### 9.2 推送链路

```python
# reports/lark.py
def send_card(card_json: dict, receiver_open_id: str) -> str:
    """
    Returns: lark message_id
    Raises: LarkSendError on failure
    """
    payload = json.dumps(card_json)
    cmd = ["lark-cli", "im", "+send-card",
           "--open-id", receiver_open_id,
           "--card", payload]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        raise LarkSendError(result.stderr)
    return parse_msg_id(result.stdout)
```

工作区 `lark-cli` 已配置；具体子命令名称在 Phase 1 实现时会查 `lark im --help` 校准。

### 9.3 Phase 2 扩展点

卡片底部预留 actions 区域，Phase 2 加 `[确认下单]` / `[忽略]` 按钮，按钮回调 webhook 由 Phase 2 负责（不在本 spec 范围）。

---

## 10. 技术栈与依赖

| 层 | 选型 | 选择理由 |
|---|---|---|
| Python | 3.11+ | 类型注解、`zoneinfo`、性能 |
| OpenD SDK | `futu-api` | Futu 官方 |
| 调度 | `APScheduler` 3.x | BlockingScheduler、cron、misfire 处理 |
| ORM | `SQLAlchemy 2.x` + `alembic` | schema 演进 |
| 配置 | `pydantic v2` + `pyyaml` | 强类型 |
| 指标计算 | self-implemented (pure pandas) | RSI(Wilder)/MACD/Bollinger 标准算法各 5-10 行；规避 pandas-ta 上游下架风险 |
| 日历 | `pandas-market-calendars` | 美股假期 + DST |
| 重试 | `tenacity` | OpenD socket 重连 |
| HTTP（备用） | `httpx` | Phase 2 webhook 用 |
| 模板 | `jinja2` | 飞书卡片 JSON 渲染 |
| CLI | `click` | 子命令 |
| 日志 | `structlog` | 结构化 |
| 测试 | `pytest` + `pytest-asyncio` + `freezegun` | 时间 mock |
| 包管理 | conda env `fin` (Python 3.11) + pip | 用户已有 miniconda；pyproject.toml 走 PEP 621 标准格式 |

---

## 11. 测试策略

| 层 | 范围 | 备注 |
|---|---|---|
| **Unit** | indicators 计算（已知 OHLCV → 已知 RSI 值）、signal 边界（=70 / <30）、compose 去重逻辑、card render 输出（snapshot 测）、config 校验、calendar（DST 切换日、假期日） | 100% 不依赖 OpenD |
| **Contract** | 所有 `data/*.py` 通过 `FutuClient` Protocol 调用；`tests/conftest.py` 提供 `FakeFutuClient` fixture | 业务逻辑全部可在无 OpenD 环境跑 |
| **Integration（可选）** | 注入 `FakeFutuClient` + in-memory SQLite，跑一次 `intraday_check` 完整 job → 校验 SQLite 落账 + 渲染出的 Card JSON | 标 `pytest -m integration` |
| **冒烟（手动）** | OpenD 起在本地时跑 `equity-monitor once --symbol US.AAPL` | README 给出步骤 |

**TDD 驱动**：每个 task 先写失败的 test，再写实现，再 commit。详见 plan。

---

## 12. CLI 接口

```bash
equity-monitor run                    # 长驻调度器
equity-monitor once --job intraday    # 立刻跑一次指定 job（调试用）
equity-monitor once --symbol US.AAPL  # 只对单个标的跑一次完整流程
equity-monitor backfill --days 30     # 回填 30 天 K 线 + 指标
equity-monitor watchlist list         # 看当前 active 标的
equity-monitor watchlist sync         # 把 yaml 同步到 SQLite symbols 表
equity-monitor db init                # 初始化 SQLite + WAL + 跑 alembic upgrade head
equity-monitor db migrate -m "msg"    # 生成新的 alembic 迁移
```

---

## 13. 错误处理

| 场景 | 行为 |
|---|---|
| OpenD 断连 | tenacity 重试 3 次；仍失败：log error + skip 当次 job + 在下次 brief 卡片中标注"数据缺失" |
| Futu Search Skill subprocess 超时 | 同上 |
| SQLite 写入失败 | log + 抛异常（让 APScheduler 标记 job failed，下次 cron 重试） |
| lark-cli 推送失败 | 重试 3 次；仍失败：保留 `signals.delivered=0`，下个 job 启动时**只重发 ts 在 6h 内的未发送 signal** |
| 无效配置（YAML schema 不通过） | 启动时 fail-fast，打印具体哪个字段不对 |
| 美股节假日 | calendar 判断后所有 job skip，不推空卡片 |

---

## 14. 安全 & 隐私

- Futu OpenD 仅监听 `127.0.0.1:11111`，不对外暴露
- 飞书 receiver `open_id` 写在 settings.yaml，gitignore 处理
- watchlist.yaml gitignore；提交 `watchlist.example.yaml`
- SQLite 文件 gitignore
- 所有日志只打印 symbol code，不打印账号 / token

---

## 15. 部署 & 运维

| 项 | 方案 |
|---|---|
| 启动 | `conda activate fin && equity-monitor run` 前台跑；用户用 `tmux` / `screen` 留驻 |
| 日志 | `data/equity_monitor.log`（轮转 by `RotatingFileHandler`，单文件 10MB × 5） |
| 健康检查 | `equity-monitor status` 子命令读 `signals.last_success_ts`，输出最近一次成功跑 job 的时间 |
| 升级 | `conda activate fin && pip install -e ".[dev]" && equity-monitor db migrate` |
| 备份 | `cp data/equity_monitor.db data/backup-$(date +%F).db` |
| 监控 | Phase 1 不接外部监控；Phase 3 加自监控告警 |

---

## 16. 与 Futu Skill 的边界

Futu skill 提供的是**飞书 / Cursor 对话内**的命令式调用。本系统**不依赖**对话调用，而是：

1. **OpenAPI 类**（实时报价、K 线、Paper Trading）：直接用 Futu 官方 `futu-api` Python SDK 接 OpenD socket，**不经 skill**
2. **Anomaly 类 + Search 类**：这些 skill 在 SKILL.md 里有 `scripts/` 目录，提供可独立调用的 Python 脚本（见 Futu skill install 文档"Anomaly Detection Skills"段落）。本系统通过 subprocess 调这些 scripts，解析 stdout JSON

OpenD 安装：本 plan 的 Task 1 调用 `/install-futu-opend` 命令完成。

---

## 17. Phase 2/3 扩展点（仅占位）

- `signals/strategy.py` (Phase 3)：策略基线模块
- `trader/paper.py` (Phase 2)：调用 Futu Paper Trading API 下单 / 撤单
- `trader/risk.py` (Phase 3)：风控规则（最大单仓、日内最大亏损、止损止盈）
- `reports/daily_review.py` (Phase 3)：每日复盘日报
- 飞书 webhook listener (Phase 2)：接收用户 confirm / cancel 回复

---

## 18. 风险 & 待解决

| 风险 | 缓解 |
|---|---|
| Futu Anomaly skill 的 scripts 调用接口稳定性未知 | Task 4-7 先做 spike：跑一次 script 看输出格式；若不稳定则 fallback 自己用 indicators 推导 |
| OpenD 偶发断连 | tenacity 重试 + 心跳监控 |
| 自实现 RSI/MACD/BOLL 与 TradingView/MetaTrader 等参考实现可能有微小差异（边界值、min_periods 截断处） | 测试用 fixture（`tests/fixtures/known_ohlcv.csv` + 单调上涨场景）锁定语义而非锁定到第 N 位小数 |
| 飞书 lark-cli 子命令名实际可能不叫 `+send-card` | Task 12 实施前先 `lark im --help` 核对；本 spec 假设值仅占位 |

---

## 19. 验收清单（Phase 1 Done 标准）

- [ ] `equity-monitor run` 长驻 24h 不崩
- [ ] watchlist 配置 ≥ 5 只标的，每只标的 24h 内至少落 ≥ 4 条 quotes、≥ 4 条 indicators
- [ ] 飞书每天能收到至少 1 条 morning_brief + 1 条 closing_brief
- [ ] 至少观察到一次 signal alert 卡片正确推送（人为构造或自然触发）
- [ ] 所有 unit test 绿
- [ ] README 包含从 0 到长驻的完整搭建步骤
- [ ] OpenD 重启后系统能自动重连恢复

---

**End of Spec**
