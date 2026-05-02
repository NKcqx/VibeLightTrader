# Equity Monitor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Mac 本地长驻一个 Python 服务，每小时拉美股监控池行情、技术指标、Futu 异动信号、新闻情绪，按规则合成信号后用飞书消息推送结构化卡片，全部历史落 SQLite，为后续半自动 / 全自动模拟操盘留好接口。

**Architecture:** APScheduler 长驻进程触发四类 cron job；数据层经 `FutuClient` Protocol 接入 OpenD 与 Futu skill scripts；signal engine 合成多源信号，按严重度分发到飞书卡片；SQLAlchemy 2.x ORM + SQLite WAL 持久化；jinja2 渲染飞书 Interactive Card → subprocess 调 lark-cli 推送。

**Tech Stack:** Python 3.11+ · conda env `fin` + pip · futu-api · APScheduler · SQLAlchemy 2.x + Alembic · pydantic v2 · pandas (RSI/MACD/BOLL self-implemented) · pandas-market-calendars · tenacity · jinja2 · click · structlog · pytest + freezegun

**Spec:** `equity-monitor/docs/superpowers/specs/2026-05-02-equity-monitor-design.md`

---

## File Inventory

### 创建（按依赖顺序）

| 路径 | 责任 | 引入于 Task |
|---|---|---|
| `equity-monitor/pyproject.toml` | PEP 621 项目元数据 + 依赖 (conda + pip 安装) | T0 |
| `equity-monitor/.gitignore` | 排除 `data/`, `config/watchlist.yaml`, `*.db`, venv | T0 |
| `equity-monitor/README.md` | 0→1 搭建步骤 | T0 (草稿) / T22 (完善) |
| `equity-monitor/scripts/install_opend.sh` | OpenD 安装引导 | T1 |
| `equity-monitor/config/watchlist.example.yaml` | 示例标的配置 | T2 |
| `equity-monitor/config/settings.yaml` | 调度 / OpenD / Lark 配置 | T2 |
| `equity-monitor/src/equity_monitor/__init__.py` | 包入口 | T0 |
| `equity-monitor/src/equity_monitor/config.py` | pydantic v2 配置加载 | T2 |
| `equity-monitor/src/equity_monitor/db.py` | SQLAlchemy session + WAL | T3 |
| `equity-monitor/src/equity_monitor/models.py` | 7 张 ORM 表 | T3 |
| `equity-monitor/alembic.ini` + `equity-monitor/alembic/` | 迁移 | T3 |
| `equity-monitor/src/equity_monitor/futu_client.py` | OpenD QuoteContext + Protocol | T4 |
| `equity-monitor/src/equity_monitor/data/quotes.py` | 实时报价落库 | T5 |
| `equity-monitor/src/equity_monitor/data/kline.py` | K 线拉取 | T6 |
| `equity-monitor/src/equity_monitor/data/indicators.py` | RSI/MACD/BOLL 计算 | T7 |
| `equity-monitor/src/equity_monitor/data/tech_anomaly.py` | Futu Technical Anomaly skill | T8 |
| `equity-monitor/src/equity_monitor/data/capital_anomaly.py` | Futu Capital Anomaly skill | T9 |
| `equity-monitor/src/equity_monitor/data/news.py` | Futu News Search | T10 |
| `equity-monitor/src/equity_monitor/data/sentiment.py` | Futu Comment Sentiment | T10 |
| `equity-monitor/src/equity_monitor/signals/base.py` | Signal dataclass + Severity enum | T11 |
| `equity-monitor/src/equity_monitor/signals/threshold.py` | 价格阈值 | T11 |
| `equity-monitor/src/equity_monitor/signals/tech.py` | RSI/MACD/BOLL 信号 | T11 |
| `equity-monitor/src/equity_monitor/signals/compose.py` | 合成 + 去重 | T12 |
| `equity-monitor/src/equity_monitor/scheduler/calendar.py` | NYSE 日历 | T13 |
| `equity-monitor/src/equity_monitor/reports/card.py` | Card schema constants | T14 |
| `equity-monitor/src/equity_monitor/reports/render.py` | jinja2 渲染 | T14 |
| `equity-monitor/src/equity_monitor/reports/templates/*.json.j2` | 卡片模板 | T14 |
| `equity-monitor/src/equity_monitor/reports/lark.py` | lark-cli subprocess | T15 |
| `equity-monitor/src/equity_monitor/scheduler/jobs.py` | 4 个 job 函数 | T16-T18 |
| `equity-monitor/src/equity_monitor/scheduler/runner.py` | APScheduler 入口 | T19 |
| `equity-monitor/src/equity_monitor/cli/main.py` | click 子命令 | T20-T21 |
| `equity-monitor/tests/**` | 单元 + 集成测试 | 各 task |

### 不修改任何工作区已有文件

---

## Task 0: 项目脚手架

**Files:**
- Create: `equity-monitor/pyproject.toml`
- Create: `equity-monitor/.gitignore`
- Create: `equity-monitor/README.md` (草稿)
- Create: `equity-monitor/src/equity_monitor/__init__.py`
- Create: `equity-monitor/tests/__init__.py`
- Create: `equity-monitor/tests/test_smoke.py`

- [ ] **Step 1: 写 pyproject.toml**

```toml
# equity-monitor/pyproject.toml
[project]
name = "equity-monitor"
version = "0.1.0"
description = "Hourly equity monitor with technical signals, news sentiment, Lark alerts and paper-trading hooks"
requires-python = ">=3.11"
authors = [{ name = "you" }]
dependencies = [
  "futu-api>=9.2.5008",
  "apscheduler>=3.10.4",
  "sqlalchemy>=2.0.30",
  "alembic>=1.13.1",
  "pydantic>=2.7.0",
  "pyyaml>=6.0.1",
  "pandas>=2.2.0",
  "pandas-market-calendars>=4.4.0",
  "tenacity>=8.2.3",
  "jinja2>=3.1.4",
  "click>=8.1.7",
  "structlog>=24.1.0",
  "httpx>=0.27.0",
]

[project.scripts]
equity-monitor = "equity_monitor.cli.main:cli"

[project.optional-dependencies]
dev = [
  "pytest>=8.2.0",
  "pytest-asyncio>=0.23.6",
  "freezegun>=1.5.0",
  "ruff>=0.4.0",
]

[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
markers = [
  "integration: end-to-end with FakeFutuClient + in-memory SQLite",
]
```

- [ ] **Step 2: 写 .gitignore**

```gitignore
# equity-monitor/.gitignore
__pycache__/
*.pyc
.venv/
.uv/
*.egg-info/
build/
dist/
.pytest_cache/
.ruff_cache/

# secrets / personal config
config/watchlist.yaml
config/settings.local.yaml

# runtime data
data/
*.db
*.db-journal
*.db-wal
*.db-shm
*.log
```

- [ ] **Step 3: 写 README.md 草稿（占位，T22 完善）**

```markdown
# Equity Monitor

Hourly US-equity monitor with technical signals, news sentiment, Lark alerts.

See `docs/superpowers/specs/2026-05-02-equity-monitor-design.md` for design.

## Quickstart (preview)

```bash
conda create -n fin python=3.11 -y
conda activate fin
pip install -e ".[dev]"
equity-monitor db init
equity-monitor run
```
```

- [ ] **Step 4: 创建包入口空文件**

```python
# equity-monitor/src/equity_monitor/__init__.py
__version__ = "0.1.0"
```

```python
# equity-monitor/tests/__init__.py
```

- [ ] **Step 5: 写一个最小 smoke test 验证导入**

```python
# equity-monitor/tests/test_smoke.py
def test_package_imports():
    import equity_monitor

    assert equity_monitor.__version__ == "0.1.0"
```

- [ ] **Step 6: 创建 conda 环境 + 安装依赖 + 跑 smoke test**

```bash
cd equity-monitor
conda create -n fin python=3.11 -y
conda activate fin
pip install -e ".[dev]"
pytest tests/test_smoke.py -v
```

Expected: `1 passed`

> 注：后续所有 task 默认在 `conda activate fin` 状态下执行；本 plan 中 `pytest`、`alembic`、`equity-monitor` 命令都假定 env 已激活。

- [ ] **Step 7: git init + first commit**

```bash
cd equity-monitor
git init -b main
git add pyproject.toml .gitignore README.md src/ tests/ docs/
git commit -m "chore: scaffold equity-monitor project"
```

---

## Task 1: OpenD 安装 + 连通验证

**Files:**
- Create: `equity-monitor/scripts/install_opend.sh`
- Create: `equity-monitor/scripts/check_opend.py`

- [ ] **Step 1: 写 install_opend.sh 引导脚本**

```bash
#!/usr/bin/env bash
# equity-monitor/scripts/install_opend.sh
set -euo pipefail

echo "==> Step 1: 调用 Futu skill /install-futu-opend"
echo "    在 Cursor / Claude Code 对话里输入: /install-futu-opend"
echo "    若 skill 未注册, 跑:"
echo "      curl -L https://openapi.futunn.com/skills/opend-skills.zip -o /tmp/opend-skills.zip"
echo "      mkdir -p ~/.claude/skills && unzip -o /tmp/opend-skills.zip -d ~/.claude/skills/"
echo "      rm /tmp/opend-skills.zip"
echo
echo "==> Step 2: 启动 OpenD (Futu app 安装好后)"
echo "    打开 OpenD GUI, 用富途牛牛账号登录, 确认监听 127.0.0.1:11111"
echo
echo "==> Step 3: 验证连接"
echo "    cd equity-monitor && python scripts/check_opend.py"
```

```bash
chmod +x equity-monitor/scripts/install_opend.sh
```

- [ ] **Step 2: 写 check_opend.py 连通验证脚本**

```python
# equity-monitor/scripts/check_opend.py
"""Smoke check: confirm OpenD is reachable and quote API works."""
from __future__ import annotations

import sys

from futu import OpenQuoteContext, RET_OK


def main() -> int:
    ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
    try:
        ret, data = ctx.get_market_snapshot(["US.AAPL"])
        if ret != RET_OK:
            print(f"FAIL: snapshot returned {ret}: {data}", file=sys.stderr)
            return 1
        print("OK: OpenD reachable")
        print(data[["code", "last_price", "update_time"]].to_string(index=False))
        return 0
    finally:
        ctx.close()


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3: 用户手动跑安装 + 验证（plan 任务里只是把步骤固化到脚本，不在 CI 跑）**

```bash
cd equity-monitor
bash scripts/install_opend.sh
# (用户按提示完成 OpenD 安装 + 登录)
python scripts/check_opend.py
```

Expected (示例)：

```
OK: OpenD reachable
   code  last_price          update_time
US.AAPL      182.30  2026-05-02 14:30:00
```

- [ ] **Step 4: Commit**

```bash
git add scripts/
git commit -m "chore: opend install guide and connectivity check script"
```

---

## Task 2: 配置加载（pydantic v2）

**Files:**
- Create: `equity-monitor/config/watchlist.example.yaml`
- Create: `equity-monitor/config/settings.yaml`
- Create: `equity-monitor/src/equity_monitor/config.py`
- Create: `equity-monitor/tests/unit/__init__.py`
- Create: `equity-monitor/tests/unit/test_config.py`

- [ ] **Step 1: 写示例 watchlist + settings**

```yaml
# equity-monitor/config/watchlist.example.yaml
symbols:
  - code: US.AAPL
    name: Apple
    upper_threshold: 200.0
    lower_threshold: 165.0
    notes: "core position"
  - code: US.NVDA
    name: NVIDIA
    upper_threshold: 150.0
    lower_threshold: 110.0
  - code: US.TSLA
    name: Tesla
```

```yaml
# equity-monitor/config/settings.yaml
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
      cron: "30 9-15 * * mon-fri"
    morning_brief:
      cron: "30 10 * * mon-fri"
    closing_brief:
      cron: "30 16 * * mon-fri"
    news_pulse:
      cron: "*/30 9-15 * * mon-fri"

lark:
  cli_path: lark-cli
  receiver:
    type: chat
    open_id: "ou_REPLACE_ME"

signals:
  rsi_overbought: 70
  rsi_oversold: 30
  bollinger_period: 20
  bollinger_std: 2
  macd_fast: 12
  macd_slow: 26
  macd_signal: 9
  dedupe_window_minutes: 60
  news_burst_drop: 3.0
  news_burst_rise: 3.0

logging:
  level: INFO
  file: data/equity_monitor.log
```

- [ ] **Step 2: 写失败的 test**

```python
# equity-monitor/tests/unit/test_config.py
from __future__ import annotations

from pathlib import Path

import pytest

from equity_monitor.config import (
    AppConfig,
    SymbolConfig,
    load_settings,
    load_watchlist,
)


def test_load_watchlist_example(tmp_path: Path) -> None:
    yml = tmp_path / "watchlist.yaml"
    yml.write_text(
        """\
symbols:
  - code: US.AAPL
    name: Apple
    upper_threshold: 200.0
    lower_threshold: 165.0
"""
    )
    wl = load_watchlist(yml)
    assert len(wl.symbols) == 1
    s: SymbolConfig = wl.symbols[0]
    assert s.code == "US.AAPL"
    assert s.upper_threshold == 200.0


def test_load_settings_full(tmp_path: Path) -> None:
    yml = tmp_path / "settings.yaml"
    yml.write_text(Path("config/settings.yaml").read_text())
    cfg: AppConfig = load_settings(yml)
    assert cfg.opend.host == "127.0.0.1"
    assert cfg.opend.port == 11111
    assert cfg.scheduler.timezone == "America/New_York"
    assert cfg.signals.rsi_overbought == 70
    assert "intraday_check" in cfg.scheduler.jobs


def test_invalid_threshold_rejected(tmp_path: Path) -> None:
    yml = tmp_path / "bad.yaml"
    yml.write_text(
        """\
symbols:
  - code: US.AAPL
    name: Apple
    upper_threshold: -5.0
"""
    )
    with pytest.raises(ValueError):
        load_watchlist(yml)
```

- [ ] **Step 3: 验证 fail**

```bash
cd equity-monitor
pytest tests/unit/test_config.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'equity_monitor.config'`

- [ ] **Step 4: 实现 config.py**

```python
# equity-monitor/src/equity_monitor/config.py
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator


class SymbolConfig(BaseModel):
    code: str = Field(pattern=r"^(US|HK|SH|SZ)\.[A-Z0-9._-]+$")
    name: str
    upper_threshold: float | None = None
    lower_threshold: float | None = None
    notes: str | None = None

    @field_validator("upper_threshold", "lower_threshold")
    @classmethod
    def _positive(cls, v: float | None) -> float | None:
        if v is not None and v <= 0:
            raise ValueError("threshold must be positive")
        return v


class WatchlistConfig(BaseModel):
    symbols: list[SymbolConfig]


class OpenDConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 11111


class DatabaseConfig(BaseModel):
    path: str
    wal_mode: bool = True


class JobCron(BaseModel):
    cron: str


class SchedulerConfig(BaseModel):
    timezone: str
    jobs: dict[str, JobCron]


class LarkReceiver(BaseModel):
    type: Literal["chat", "user"]
    open_id: str


class LarkConfig(BaseModel):
    cli_path: str = "lark-cli"
    receiver: LarkReceiver


class SignalsConfig(BaseModel):
    rsi_overbought: float = 70
    rsi_oversold: float = 30
    bollinger_period: int = 20
    bollinger_std: float = 2
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    dedupe_window_minutes: int = 60
    news_burst_drop: float = 3.0
    news_burst_rise: float = 3.0


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: str | None = None


class AppConfig(BaseModel):
    opend: OpenDConfig
    database: DatabaseConfig
    scheduler: SchedulerConfig
    lark: LarkConfig
    signals: SignalsConfig
    logging: LoggingConfig


def load_watchlist(path: str | Path) -> WatchlistConfig:
    data = yaml.safe_load(Path(path).read_text())
    return WatchlistConfig.model_validate(data)


def load_settings(path: str | Path) -> AppConfig:
    data = yaml.safe_load(Path(path).read_text())
    return AppConfig.model_validate(data)
```

- [ ] **Step 5: 验证 pass**

```bash
pytest tests/unit/test_config.py -v
```

Expected: `3 passed`

- [ ] **Step 6: Commit**

```bash
git add config/ src/equity_monitor/config.py tests/unit/__init__.py tests/unit/test_config.py
git commit -m "feat(config): pydantic v2 loader for watchlist and settings"
```

---

## Task 3: SQLite + ORM models + Alembic

**Files:**
- Create: `equity-monitor/src/equity_monitor/models.py`
- Create: `equity-monitor/src/equity_monitor/db.py`
- Create: `equity-monitor/alembic.ini`
- Create: `equity-monitor/alembic/env.py`
- Create: `equity-monitor/alembic/script.py.mako`
- Create: `equity-monitor/alembic/versions/.gitkeep`
- Create: `equity-monitor/tests/unit/test_db.py`
- Create: `equity-monitor/tests/conftest.py`

- [ ] **Step 1: 写 models.py（7 张表）**

```python
# equity-monitor/src/equity_monitor/models.py
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Symbol(Base):
    __tablename__ = "symbols"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    market: Mapped[str] = mapped_column(String, nullable=False, default="US")
    currency: Mapped[str] = mapped_column(String, nullable=False, default="USD")
    lot_size: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    upper_threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    lower_threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Quote(Base):
    __tablename__ = "quotes"
    __table_args__ = (
        UniqueConstraint("symbol_id", "ts", name="uq_quotes_symbol_ts"),
        Index("idx_quotes_symbol_ts", "symbol_id", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey("symbols.id"), nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[int] = mapped_column(Integer, nullable=False)
    turnover: Mapped[float] = mapped_column(Float, nullable=False)


class Indicator(Base):
    __tablename__ = "indicators"
    __table_args__ = (
        UniqueConstraint("symbol_id", "ts", name="uq_indicators_symbol_ts"),
        Index("idx_indicators_symbol_ts", "symbol_id", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey("symbols.id"), nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    rsi_14: Mapped[float | None] = mapped_column(Float, nullable=True)
    macd: Mapped[float | None] = mapped_column(Float, nullable=True)
    macd_signal: Mapped[float | None] = mapped_column(Float, nullable=True)
    macd_hist: Mapped[float | None] = mapped_column(Float, nullable=True)
    boll_upper: Mapped[float | None] = mapped_column(Float, nullable=True)
    boll_mid: Mapped[float | None] = mapped_column(Float, nullable=True)
    boll_lower: Mapped[float | None] = mapped_column(Float, nullable=True)


class Signal(Base):
    __tablename__ = "signals"
    __table_args__ = (
        UniqueConstraint(
            "symbol_id", "ts", "signal_type", name="uq_signals_symbol_ts_type"
        ),
        Index("idx_signals_symbol_ts", "symbol_id", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey("symbols.id"), nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    signal_type: Mapped[str] = mapped_column(String, nullable=False)
    severity: Mapped[str] = mapped_column(String, nullable=False)  # INFO/WARN/CRITICAL
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    delivered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    delivery_ts: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    delivery_msg_id: Mapped[str | None] = mapped_column(String, nullable=True)


class NewsDigest(Base):
    __tablename__ = "news_digest"
    __table_args__ = (
        UniqueConstraint("symbol_id", "url", name="uq_news_symbol_url"),
        Index("idx_news_symbol_ts", "symbol_id", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey("symbols.id"), nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    source: Mapped[str | None] = mapped_column(String, nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    sentiment_score: Mapped[float | None] = mapped_column(Float, nullable=True)


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey("symbols.id"), nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    side: Mapped[str] = mapped_column(String, nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    futu_order_id: Mapped[str | None] = mapped_column(String, nullable=True)
    signal_id: Mapped[int | None] = mapped_column(
        ForeignKey("signals.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String, nullable=False)


class Position(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol_id: Mapped[int] = mapped_column(
        ForeignKey("symbols.id"), unique=True, nullable=False
    )
    qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    avg_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    unrealized_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    realized_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
```

- [ ] **Step 2: 写 db.py**

```python
# equity-monitor/src/equity_monitor/db.py
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from equity_monitor.models import Base


def make_engine(db_path: str | Path, *, wal_mode: bool = True) -> Engine:
    url = f"sqlite:///{db_path}"
    engine = create_engine(url, future=True)

    if wal_mode:

        @event.listens_for(engine, "connect")
        def _enable_wal(dbapi_conn, _):  # type: ignore[no-untyped-def]
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def init_schema(engine: Engine) -> None:
    Base.metadata.create_all(engine)


def make_sessionmaker(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
```

- [ ] **Step 3: 写 conftest.py 提供共享 fixture**

```python
# equity-monitor/tests/conftest.py
from __future__ import annotations

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from equity_monitor.db import init_schema, make_engine, make_sessionmaker


@pytest.fixture
def engine(tmp_path) -> Engine:
    db = tmp_path / "test.db"
    eng = make_engine(db, wal_mode=False)
    init_schema(eng)
    return eng


@pytest.fixture
def factory(engine: Engine) -> sessionmaker[Session]:
    return make_sessionmaker(engine)
```

- [ ] **Step 4: 写失败的 db test**

```python
# equity-monitor/tests/unit/test_db.py
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session, sessionmaker

from equity_monitor.db import session_scope
from equity_monitor.models import Quote, Symbol


def test_can_insert_symbol_and_quote(factory: sessionmaker[Session]) -> None:
    with session_scope(factory) as s:
        sym = Symbol(code="US.AAPL", name="Apple")
        s.add(sym)
        s.flush()
        s.add(
            Quote(
                symbol_id=sym.id,
                ts=datetime(2026, 5, 2, 14, 30, tzinfo=timezone.utc).replace(tzinfo=None),
                open=180.0,
                high=183.0,
                low=179.5,
                close=182.3,
                volume=12_000_000,
                turnover=2_184_000_000.0,
            )
        )

    with session_scope(factory) as s:
        quotes = s.query(Quote).all()
        assert len(quotes) == 1
        assert quotes[0].close == 182.3


def test_unique_constraint_quote(factory: sessionmaker[Session]) -> None:
    import pytest
    from sqlalchemy.exc import IntegrityError

    ts = datetime(2026, 5, 2, 14, 30)
    with session_scope(factory) as s:
        sym = Symbol(code="US.AAPL", name="Apple")
        s.add(sym)
        s.flush()
        s.add(
            Quote(
                symbol_id=sym.id, ts=ts, open=1, high=1, low=1, close=1, volume=1, turnover=1
            )
        )

    with pytest.raises(IntegrityError):
        with session_scope(factory) as s:
            sym = s.query(Symbol).first()
            assert sym is not None
            s.add(
                Quote(
                    symbol_id=sym.id,
                    ts=ts,
                    open=2,
                    high=2,
                    low=2,
                    close=2,
                    volume=2,
                    turnover=2,
                )
            )
```

- [ ] **Step 5: 验证 fail → 实现已写完 → 验证 pass**

```bash
pytest tests/unit/test_db.py -v
```

Expected: `2 passed`

- [ ] **Step 6: 初始化 Alembic**

```bash
cd equity-monitor
alembic init alembic
```

编辑 `alembic.ini` 第一个 `sqlalchemy.url` 行替换为：

```ini
sqlalchemy.url = sqlite:///data/equity_monitor.db
```

编辑 `alembic/env.py`，把模块 import + target_metadata 设上：

```python
# alembic/env.py — 在 target_metadata = None 之前加
from equity_monitor.models import Base
target_metadata = Base.metadata
```

- [ ] **Step 7: 生成首个 migration**

```bash
mkdir -p data
alembic revision --autogenerate -m "initial schema"
alembic upgrade head
ls data/equity_monitor.db   # 文件应已创建
```

- [ ] **Step 8: Commit**

```bash
git add src/equity_monitor/models.py src/equity_monitor/db.py \
        alembic.ini alembic/ tests/conftest.py tests/unit/test_db.py
git commit -m "feat(db): SQLAlchemy 2.x models, WAL engine, alembic init"
```

---

## Task 4: FutuClient Protocol + 实现 + Fake

**Files:**
- Create: `equity-monitor/src/equity_monitor/futu_client.py`
- Modify: `equity-monitor/tests/conftest.py` (加 FakeFutuClient fixture)
- Create: `equity-monitor/tests/unit/test_futu_client_fake.py`

- [ ] **Step 1: 写 futu_client.py**

```python
# equity-monitor/src/equity_monitor/futu_client.py
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from tenacity import retry, stop_after_attempt, wait_exponential


@dataclass(frozen=True, slots=True)
class Snapshot:
    code: str
    last_price: float
    open_price: float
    high_price: float
    low_price: float
    volume: int
    turnover: float
    update_time: datetime


@dataclass(frozen=True, slots=True)
class Candle:
    code: str
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    turnover: float


class FutuClient(Protocol):
    def snapshot(self, codes: Sequence[str]) -> list[Snapshot]: ...
    def kline(
        self,
        code: str,
        *,
        ktype: str,  # "K_60M" | "K_DAY"
        limit: int,
    ) -> list[Candle]: ...
    def close(self) -> None: ...


class OpenDClient:
    """Real client backed by futu-api OpenQuoteContext."""

    def __init__(self, host: str = "127.0.0.1", port: int = 11111) -> None:
        from futu import OpenQuoteContext

        self._ctx = OpenQuoteContext(host=host, port=port)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    def snapshot(self, codes: Sequence[str]) -> list[Snapshot]:
        from futu import RET_OK

        ret, df = self._ctx.get_market_snapshot(list(codes))
        if ret != RET_OK:
            raise RuntimeError(f"snapshot failed: {df}")
        out: list[Snapshot] = []
        for _, row in df.iterrows():
            out.append(
                Snapshot(
                    code=row["code"],
                    last_price=float(row["last_price"]),
                    open_price=float(row["open_price"]),
                    high_price=float(row["high_price"]),
                    low_price=float(row["low_price"]),
                    volume=int(row["volume"]),
                    turnover=float(row["turnover"]),
                    update_time=datetime.fromisoformat(str(row["update_time"])),
                )
            )
        return out

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    def kline(self, code: str, *, ktype: str, limit: int) -> list[Candle]:
        from futu import KLType, RET_OK

        kt = {"K_60M": KLType.K_60M, "K_DAY": KLType.K_DAY}[ktype]
        ret, df, _ = self._ctx.request_history_kline(
            code, ktype=kt, max_count=limit
        )
        if ret != RET_OK:
            raise RuntimeError(f"kline failed: {df}")
        out: list[Candle] = []
        for _, row in df.iterrows():
            out.append(
                Candle(
                    code=code,
                    ts=datetime.fromisoformat(str(row["time_key"])),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=int(row["volume"]),
                    turnover=float(row["turnover"]),
                )
            )
        return out

    def close(self) -> None:
        self._ctx.close()


class FakeFutuClient:
    """In-memory fake for tests. Caller pre-loads snapshots / candles."""

    def __init__(self) -> None:
        self._snapshots: dict[str, Snapshot] = {}
        self._klines: dict[tuple[str, str], list[Candle]] = {}
        self.closed = False

    def set_snapshot(self, snap: Snapshot) -> None:
        self._snapshots[snap.code] = snap

    def set_kline(self, code: str, ktype: str, candles: list[Candle]) -> None:
        self._klines[(code, ktype)] = list(candles)

    def snapshot(self, codes: Sequence[str]) -> list[Snapshot]:
        return [self._snapshots[c] for c in codes if c in self._snapshots]

    def kline(self, code: str, *, ktype: str, limit: int) -> list[Candle]:
        return self._klines.get((code, ktype), [])[-limit:]

    def close(self) -> None:
        self.closed = True
```

- [ ] **Step 2: 在 conftest.py 增加 fake_futu fixture**

追加到 `tests/conftest.py`:

```python
# tests/conftest.py — 追加
from equity_monitor.futu_client import FakeFutuClient


@pytest.fixture
def fake_futu() -> FakeFutuClient:
    return FakeFutuClient()
```

- [ ] **Step 3: 写 fake 行为 test**

```python
# equity-monitor/tests/unit/test_futu_client_fake.py
from __future__ import annotations

from datetime import datetime

from equity_monitor.futu_client import Candle, FakeFutuClient, Snapshot


def test_fake_snapshot_roundtrip(fake_futu: FakeFutuClient) -> None:
    fake_futu.set_snapshot(
        Snapshot(
            code="US.AAPL",
            last_price=182.3,
            open_price=180.0,
            high_price=183.0,
            low_price=179.5,
            volume=12_000_000,
            turnover=2.184e9,
            update_time=datetime(2026, 5, 2, 14, 30),
        )
    )
    out = fake_futu.snapshot(["US.AAPL"])
    assert len(out) == 1 and out[0].last_price == 182.3


def test_fake_kline_limit(fake_futu: FakeFutuClient) -> None:
    candles = [
        Candle(
            code="US.AAPL",
            ts=datetime(2026, 5, 2, h, 30),
            open=180.0 + h,
            high=181.0 + h,
            low=179.0 + h,
            close=180.5 + h,
            volume=10_000,
            turnover=1.8e6,
        )
        for h in range(10, 16)
    ]
    fake_futu.set_kline("US.AAPL", "K_60M", candles)
    out = fake_futu.kline("US.AAPL", ktype="K_60M", limit=3)
    assert len(out) == 3
    assert [c.ts.hour for c in out] == [13, 14, 15]
```

- [ ] **Step 4: 验证 pass**

```bash
pytest tests/unit/test_futu_client_fake.py -v
```

Expected: `2 passed`

- [ ] **Step 5: Commit**

```bash
git add src/equity_monitor/futu_client.py tests/conftest.py tests/unit/test_futu_client_fake.py
git commit -m "feat(futu): Protocol-based FutuClient with OpenD impl and FakeFutuClient"
```

---

## Task 5: data.quotes — 实时报价落库

**Files:**
- Create: `equity-monitor/src/equity_monitor/data/__init__.py`
- Create: `equity-monitor/src/equity_monitor/data/quotes.py`
- Create: `equity-monitor/tests/unit/test_data_quotes.py`

- [ ] **Step 1: 写失败的 test**

```python
# equity-monitor/tests/unit/test_data_quotes.py
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import sessionmaker

from equity_monitor.data.quotes import sync_snapshots
from equity_monitor.db import session_scope
from equity_monitor.futu_client import FakeFutuClient, Snapshot
from equity_monitor.models import Quote, Symbol


def test_sync_snapshots_inserts_quote(factory: sessionmaker, fake_futu: FakeFutuClient) -> None:
    with session_scope(factory) as s:
        s.add(Symbol(code="US.AAPL", name="Apple"))

    fake_futu.set_snapshot(
        Snapshot(
            code="US.AAPL",
            last_price=182.3,
            open_price=180.0,
            high_price=183.0,
            low_price=179.5,
            volume=12_000_000,
            turnover=2.184e9,
            update_time=datetime(2026, 5, 2, 14, 30),
        )
    )

    inserted = sync_snapshots(fake_futu, factory, codes=["US.AAPL"])
    assert inserted == 1

    with session_scope(factory) as s:
        q = s.query(Quote).one()
        assert q.close == 182.3
        assert q.open == 180.0


def test_sync_snapshots_idempotent(factory: sessionmaker, fake_futu: FakeFutuClient) -> None:
    with session_scope(factory) as s:
        s.add(Symbol(code="US.AAPL", name="Apple"))

    fake_futu.set_snapshot(
        Snapshot(
            code="US.AAPL",
            last_price=182.3,
            open_price=180.0,
            high_price=183.0,
            low_price=179.5,
            volume=12_000_000,
            turnover=2.184e9,
            update_time=datetime(2026, 5, 2, 14, 30),
        )
    )

    n1 = sync_snapshots(fake_futu, factory, codes=["US.AAPL"])
    n2 = sync_snapshots(fake_futu, factory, codes=["US.AAPL"])
    assert n1 == 1
    assert n2 == 0  # same ts → ON CONFLICT skip

    with session_scope(factory) as s:
        assert s.query(Quote).count() == 1
```

- [ ] **Step 2: 验证 fail**

```bash
pytest tests/unit/test_data_quotes.py -v
```

Expected: `ModuleNotFoundError`

- [ ] **Step 3: 实现 data/quotes.py**

```python
# equity-monitor/src/equity_monitor/data/__init__.py
```

```python
# equity-monitor/src/equity_monitor/data/quotes.py
from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import sessionmaker

from equity_monitor.db import session_scope
from equity_monitor.futu_client import FutuClient
from equity_monitor.models import Quote, Symbol


def sync_snapshots(
    client: FutuClient,
    factory: sessionmaker,
    *,
    codes: Sequence[str],
) -> int:
    """Pull snapshots for `codes` and upsert into `quotes`. Returns rows actually inserted."""
    snaps = client.snapshot(codes)
    inserted = 0
    with session_scope(factory) as session:
        sym_map = {
            s.code: s.id
            for s in session.query(Symbol).filter(Symbol.code.in_(codes)).all()
        }
        for snap in snaps:
            sym_id = sym_map.get(snap.code)
            if sym_id is None:
                continue
            stmt = (
                insert(Quote)
                .values(
                    symbol_id=sym_id,
                    ts=snap.update_time,
                    open=snap.open_price,
                    high=snap.high_price,
                    low=snap.low_price,
                    close=snap.last_price,
                    volume=snap.volume,
                    turnover=snap.turnover,
                )
                .on_conflict_do_nothing(index_elements=["symbol_id", "ts"])
            )
            result = session.execute(stmt)
            if result.rowcount > 0:
                inserted += 1
    return inserted
```

- [ ] **Step 4: 验证 pass**

```bash
pytest tests/unit/test_data_quotes.py -v
```

Expected: `2 passed`

- [ ] **Step 5: Commit**

```bash
git add src/equity_monitor/data/ tests/unit/test_data_quotes.py
git commit -m "feat(data): sync_snapshots upserts realtime quotes"
```

---

## Task 6: data.kline — 历史 K 线拉取

**Files:**
- Create: `equity-monitor/src/equity_monitor/data/kline.py`
- Create: `equity-monitor/tests/unit/test_data_kline.py`

- [ ] **Step 1: 写失败 test**

```python
# equity-monitor/tests/unit/test_data_kline.py
from __future__ import annotations

from datetime import datetime

from equity_monitor.data.kline import fetch_kline_df
from equity_monitor.futu_client import Candle, FakeFutuClient


def test_fetch_kline_returns_dataframe(fake_futu: FakeFutuClient) -> None:
    candles = [
        Candle(
            code="US.AAPL",
            ts=datetime(2026, 5, 2, h, 30),
            open=180.0 + h,
            high=181.0 + h,
            low=179.0 + h,
            close=180.5 + h,
            volume=10_000,
            turnover=1.8e6,
        )
        for h in range(10, 16)
    ]
    fake_futu.set_kline("US.AAPL", "K_60M", candles)

    df = fetch_kline_df(fake_futu, "US.AAPL", ktype="K_60M", limit=6)
    assert list(df.columns) == ["open", "high", "low", "close", "volume", "turnover"]
    assert len(df) == 6
    assert df.index.name == "ts"
    assert df["close"].iloc[-1] == 180.5 + 15
```

- [ ] **Step 2: 实现 data/kline.py**

```python
# equity-monitor/src/equity_monitor/data/kline.py
from __future__ import annotations

import pandas as pd

from equity_monitor.futu_client import FutuClient


def fetch_kline_df(
    client: FutuClient,
    code: str,
    *,
    ktype: str = "K_60M",
    limit: int = 200,
) -> pd.DataFrame:
    """Return a tidy OHLCV DataFrame indexed by ts (ascending)."""
    candles = client.kline(code, ktype=ktype, limit=limit)
    if not candles:
        return pd.DataFrame(
            columns=["open", "high", "low", "close", "volume", "turnover"]
        )
    rows = [
        {
            "ts": c.ts,
            "open": c.open,
            "high": c.high,
            "low": c.low,
            "close": c.close,
            "volume": c.volume,
            "turnover": c.turnover,
        }
        for c in candles
    ]
    df = pd.DataFrame(rows).set_index("ts").sort_index()
    return df
```

- [ ] **Step 3: 验证 pass + commit**

```bash
pytest tests/unit/test_data_kline.py -v
git add src/equity_monitor/data/kline.py tests/unit/test_data_kline.py
git commit -m "feat(data): fetch_kline_df returns OHLCV pandas DataFrame"
```

---

## Task 7: data.indicators — RSI/MACD/BOLL

**Files:**
- Create: `equity-monitor/src/equity_monitor/data/indicators.py`
- Create: `equity-monitor/tests/fixtures/__init__.py`
- Create: `equity-monitor/tests/fixtures/known_ohlc.csv`
- Create: `equity-monitor/tests/unit/test_indicators.py`

- [ ] **Step 1: 准备已知输入 fixture（30 根 K 线，构造单调上涨场景验证 RSI 应高）**

```csv
# equity-monitor/tests/fixtures/known_ohlc.csv
ts,open,high,low,close,volume,turnover
2026-04-01 09:30,100.0,101.5,99.5,101.0,10000,1010000
2026-04-01 10:30,101.0,102.5,100.5,102.0,10000,1020000
2026-04-01 11:30,102.0,103.5,101.5,103.0,10000,1030000
2026-04-01 12:30,103.0,104.5,102.5,104.0,10000,1040000
2026-04-01 13:30,104.0,105.5,103.5,105.0,10000,1050000
2026-04-01 14:30,105.0,106.5,104.5,106.0,10000,1060000
2026-04-01 15:30,106.0,107.5,105.5,107.0,10000,1070000
2026-04-02 09:30,107.0,108.5,106.5,108.0,10000,1080000
2026-04-02 10:30,108.0,109.5,107.5,109.0,10000,1090000
2026-04-02 11:30,109.0,110.5,108.5,110.0,10000,1100000
2026-04-02 12:30,110.0,111.5,109.5,111.0,10000,1110000
2026-04-02 13:30,111.0,112.5,110.5,112.0,10000,1120000
2026-04-02 14:30,112.0,113.5,111.5,113.0,10000,1130000
2026-04-02 15:30,113.0,114.5,112.5,114.0,10000,1140000
2026-04-03 09:30,114.0,115.5,113.5,115.0,10000,1150000
2026-04-03 10:30,115.0,116.5,114.5,116.0,10000,1160000
2026-04-03 11:30,116.0,117.5,115.5,117.0,10000,1170000
2026-04-03 12:30,117.0,118.5,116.5,118.0,10000,1180000
2026-04-03 13:30,118.0,119.5,117.5,119.0,10000,1190000
2026-04-03 14:30,119.0,120.5,118.5,120.0,10000,1200000
2026-04-03 15:30,120.0,121.5,119.5,121.0,10000,1210000
2026-04-06 09:30,121.0,122.5,120.5,122.0,10000,1220000
2026-04-06 10:30,122.0,123.5,121.5,123.0,10000,1230000
2026-04-06 11:30,123.0,124.5,122.5,124.0,10000,1240000
2026-04-06 12:30,124.0,125.5,123.5,125.0,10000,1250000
2026-04-06 13:30,125.0,126.5,124.5,126.0,10000,1260000
2026-04-06 14:30,126.0,127.5,125.5,127.0,10000,1270000
2026-04-06 15:30,127.0,128.5,126.5,128.0,10000,1280000
2026-04-07 09:30,128.0,129.5,127.5,129.0,10000,1290000
2026-04-07 10:30,129.0,130.5,128.5,130.0,10000,1300000
```

- [ ] **Step 2: 写失败 test**

```python
# equity-monitor/tests/unit/test_indicators.py
from __future__ import annotations

from pathlib import Path

import pandas as pd

from equity_monitor.data.indicators import compute_indicators

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "known_ohlc.csv"


def _load() -> pd.DataFrame:
    df = pd.read_csv(FIXTURE, parse_dates=["ts"]).set_index("ts").sort_index()
    return df


def test_compute_indicators_columns_and_length() -> None:
    df = _load()
    out = compute_indicators(df)
    expected = {"rsi_14", "macd", "macd_signal", "macd_hist", "boll_upper", "boll_mid", "boll_lower"}
    assert expected.issubset(out.columns)
    assert len(out) == len(df)


def test_rsi_high_in_uptrend() -> None:
    df = _load()
    out = compute_indicators(df)
    assert out["rsi_14"].iloc[-1] > 70.0  # monotonic up → overbought


def test_macd_positive_in_uptrend() -> None:
    df = _load()
    out = compute_indicators(df)
    assert out["macd"].iloc[-1] > 0
    assert out["macd_hist"].iloc[-1] > 0


def test_boll_mid_equals_sma() -> None:
    df = _load()
    out = compute_indicators(df, boll_period=20)
    sma = df["close"].rolling(20).mean()
    pd.testing.assert_series_equal(
        out["boll_mid"].dropna(),
        sma.dropna(),
        check_names=False,
    )
```

- [ ] **Step 3: 实现 data/indicators.py（pure pandas/numpy，无外部 TA 库依赖）**

> **背景**：原 plan 用 `pandas-ta`，但其上游已把所有 0.3.x 历史版本从 PyPI 下架，剩下的 0.4.x 仅支持 Python ≥3.12。我们用 pure pandas/numpy 自行实现 RSI（Wilder 平滑）/ MACD / Bollinger Bands，三个都是 standard 算法，无需第三方 TA 库。

```python
# equity-monitor/src/equity_monitor/data/indicators.py
from __future__ import annotations

import pandas as pd


def _rsi(close: pd.Series, period: int) -> pd.Series:
    """Wilder's RSI (standard formulation, matches TradingView/MetaTrader default)."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, pd.NA)
    rsi = 100.0 - 100.0 / (1.0 + rs)
    return rsi


def _macd(close: pd.Series, fast: int, slow: int, signal: int) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Standard MACD (EMA fast - EMA slow), signal = EMA of MACD line, hist = MACD - signal."""
    ema_fast = close.ewm(span=fast, adjust=False, min_periods=fast).mean()
    ema_slow = close.ewm(span=slow, adjust=False, min_periods=slow).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def _bollinger(close: pd.Series, period: int, std_mult: float) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Bollinger Bands: SMA ± std_mult * rolling population stddev."""
    mid = close.rolling(period).mean()
    std = close.rolling(period).std(ddof=0)
    upper = mid + std_mult * std
    lower = mid - std_mult * std
    return lower, mid, upper


def compute_indicators(
    df: pd.DataFrame,
    *,
    rsi_period: int = 14,
    macd_fast: int = 12,
    macd_slow: int = 26,
    macd_signal: int = 9,
    boll_period: int = 20,
    boll_std: float = 2.0,
) -> pd.DataFrame:
    """Compute RSI / MACD / Bollinger from OHLC DataFrame.

    Returns a DataFrame indexed identically to `df` with the original columns
    plus rsi_14, macd, macd_signal, macd_hist, boll_upper, boll_mid, boll_lower.
    """
    out = df.copy()
    out["rsi_14"] = _rsi(out["close"], period=rsi_period)
    macd_line, sig_line, hist = _macd(
        out["close"], fast=macd_fast, slow=macd_slow, signal=macd_signal
    )
    out["macd"] = macd_line
    out["macd_signal"] = sig_line
    out["macd_hist"] = hist
    lower, mid, upper = _bollinger(out["close"], period=boll_period, std_mult=boll_std)
    out["boll_lower"] = lower
    out["boll_mid"] = mid
    out["boll_upper"] = upper
    return out
```

- [ ] **Step 4: 验证 pass + commit**

```bash
pytest tests/unit/test_indicators.py -v
git add src/equity_monitor/data/indicators.py tests/fixtures/ tests/unit/test_indicators.py
git commit -m "feat(data): compute_indicators (RSI Wilder, MACD, Bollinger) pure pandas"
```

---

## Task 8: data.tech_anomaly — Futu Technical Anomaly subprocess

**Files:**
- Create: `equity-monitor/src/equity_monitor/data/tech_anomaly.py`
- Create: `equity-monitor/tests/unit/test_data_tech_anomaly.py`

> **Spike note for implementer:** 在写测试前先在终端跑一次 Futu Technical Anomaly skill 的脚本（按 `~/.cursor/rules/futu-technical-anomaly.md` 指示），把真实输出 stdout JSON 抓下来贴进 test fixture 里。下面给出预期 schema 的 typed parser，未来如果脚本输出 schema 微调，只改 dataclass 字段即可。

- [ ] **Step 1: 写 dataclass + parser**

```python
# equity-monitor/src/equity_monitor/data/tech_anomaly.py
from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True, slots=True)
class TechAnomaly:
    code: str
    ts: datetime
    indicator: str       # "MACD" | "RSI" | "KDJ" | "BOLL" | "MA" | "PATTERN"
    event: str           # "golden_cross" | "death_cross" | "overbought" | "oversold" | "M_top" | ...
    description: str


def _parse(payload: dict) -> list[TechAnomaly]:
    out: list[TechAnomaly] = []
    for item in payload.get("anomalies", []):
        out.append(
            TechAnomaly(
                code=item["code"],
                ts=datetime.fromisoformat(item["ts"]),
                indicator=item["indicator"],
                event=item["event"],
                description=item.get("description", ""),
            )
        )
    return out


def fetch_tech_anomalies(
    codes: Sequence[str],
    *,
    script_path: str | Path = "~/.cursor/skills/futu-technical-anomaly/scripts/run.py",
    timeout: int = 30,
) -> list[TechAnomaly]:
    """Invoke Futu Technical Anomaly script via subprocess; parse stdout JSON."""
    cmd = ["python", str(Path(script_path).expanduser()), "--codes", ",".join(codes)]
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"tech_anomaly script failed: {result.stderr}")
    return _parse(json.loads(result.stdout))
```

- [ ] **Step 2: 写失败 test（mock subprocess）**

```python
# equity-monitor/tests/unit/test_data_tech_anomaly.py
from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import patch

from equity_monitor.data.tech_anomaly import _parse, fetch_tech_anomalies


def test_parse_extracts_anomalies() -> None:
    payload = {
        "anomalies": [
            {
                "code": "US.NVDA",
                "ts": "2026-05-02T14:30:00",
                "indicator": "MACD",
                "event": "death_cross",
                "description": "MACD 柱由正转负",
            }
        ]
    }
    out = _parse(payload)
    assert len(out) == 1
    assert out[0].event == "death_cross"
    assert out[0].ts == datetime(2026, 5, 2, 14, 30)


def test_fetch_invokes_subprocess_and_parses() -> None:
    fake_stdout = json.dumps(
        {
            "anomalies": [
                {
                    "code": "US.AAPL",
                    "ts": "2026-05-02T14:30:00",
                    "indicator": "RSI",
                    "event": "overbought",
                    "description": "RSI=72",
                }
            ]
        }
    )
    with patch("equity_monitor.data.tech_anomaly.subprocess.run") as run:
        run.return_value.returncode = 0
        run.return_value.stdout = fake_stdout
        run.return_value.stderr = ""
        out = fetch_tech_anomalies(["US.AAPL"], script_path="/fake/run.py")
    assert len(out) == 1
    assert out[0].indicator == "RSI"
```

- [ ] **Step 3: 验证 pass + commit**

```bash
pytest tests/unit/test_data_tech_anomaly.py -v
git add src/equity_monitor/data/tech_anomaly.py tests/unit/test_data_tech_anomaly.py
git commit -m "feat(data): tech_anomaly subprocess wrapper for Futu Technical Anomaly skill"
```

---

## Task 9: data.capital_anomaly — Futu Capital Anomaly subprocess

**Files:**
- Create: `equity-monitor/src/equity_monitor/data/capital_anomaly.py`
- Create: `equity-monitor/tests/unit/test_data_capital_anomaly.py`

- [ ] **Step 1: 实现（类比 T8）**

```python
# equity-monitor/src/equity_monitor/data/capital_anomaly.py
from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True, slots=True)
class CapitalAnomaly:
    code: str
    ts: datetime
    flow_type: str   # "main_inflow" | "main_outflow" | "block_buy" | "block_sell" | "short_burst"
    amount: float    # 净流入/出额，正负
    description: str


def _parse(payload: dict) -> list[CapitalAnomaly]:
    out: list[CapitalAnomaly] = []
    for item in payload.get("anomalies", []):
        out.append(
            CapitalAnomaly(
                code=item["code"],
                ts=datetime.fromisoformat(item["ts"]),
                flow_type=item["flow_type"],
                amount=float(item.get("amount", 0.0)),
                description=item.get("description", ""),
            )
        )
    return out


def fetch_capital_anomalies(
    codes: Sequence[str],
    *,
    script_path: str | Path = "~/.cursor/skills/futu-capital-anomaly/scripts/run.py",
    timeout: int = 30,
) -> list[CapitalAnomaly]:
    cmd = ["python", str(Path(script_path).expanduser()), "--codes", ",".join(codes)]
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"capital_anomaly script failed: {result.stderr}")
    return _parse(json.loads(result.stdout))
```

- [ ] **Step 2: 写 test 并验证**

```python
# equity-monitor/tests/unit/test_data_capital_anomaly.py
from __future__ import annotations

from datetime import datetime

from equity_monitor.data.capital_anomaly import _parse


def test_parse_capital_anomaly() -> None:
    payload = {
        "anomalies": [
            {
                "code": "US.NVDA",
                "ts": "2026-05-02T14:30:00",
                "flow_type": "main_outflow",
                "amount": -12_400_000.0,
                "description": "主力净流出 12.4M",
            }
        ]
    }
    out = _parse(payload)
    assert len(out) == 1
    assert out[0].flow_type == "main_outflow"
    assert out[0].amount == -12_400_000.0
    assert out[0].ts == datetime(2026, 5, 2, 14, 30)
```

```bash
pytest tests/unit/test_data_capital_anomaly.py -v
git add src/equity_monitor/data/capital_anomaly.py tests/unit/test_data_capital_anomaly.py
git commit -m "feat(data): capital_anomaly subprocess wrapper"
```

---

## Task 10: data.news + data.sentiment — Futu Search Skills

**Files:**
- Create: `equity-monitor/src/equity_monitor/data/news.py`
- Create: `equity-monitor/src/equity_monitor/data/sentiment.py`
- Create: `equity-monitor/tests/unit/test_data_news.py`
- Create: `equity-monitor/tests/unit/test_data_sentiment.py`

- [ ] **Step 1: 写 news.py**

```python
# equity-monitor/src/equity_monitor/data/news.py
from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True, slots=True)
class NewsItem:
    code: str
    ts: datetime
    source: str | None
    title: str
    url: str
    summary: str | None


def _parse(payload: dict) -> list[NewsItem]:
    out: list[NewsItem] = []
    for code, items in payload.get("by_code", {}).items():
        for it in items:
            out.append(
                NewsItem(
                    code=code,
                    ts=datetime.fromisoformat(it["ts"]),
                    source=it.get("source"),
                    title=it["title"],
                    url=it["url"],
                    summary=it.get("summary"),
                )
            )
    return out


def fetch_news_digest(
    codes: Sequence[str],
    *,
    script_path: str | Path = "~/.cursor/skills/futu-stock-digest/scripts/run.py",
    timeout: int = 60,
) -> list[NewsItem]:
    cmd = ["python", str(Path(script_path).expanduser()), "--codes", ",".join(codes)]
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"news script failed: {result.stderr}")
    return _parse(json.loads(result.stdout))
```

- [ ] **Step 2: 写 sentiment.py**

```python
# equity-monitor/src/equity_monitor/data/sentiment.py
from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SentimentSnapshot:
    code: str
    ts: datetime
    temperature: float   # 0–10
    bullish_pct: float
    bearish_pct: float
    sample_size: int


def _parse(payload: dict) -> list[SentimentSnapshot]:
    out: list[SentimentSnapshot] = []
    for item in payload.get("snapshots", []):
        out.append(
            SentimentSnapshot(
                code=item["code"],
                ts=datetime.fromisoformat(item["ts"]),
                temperature=float(item["temperature"]),
                bullish_pct=float(item.get("bullish_pct", 0.0)),
                bearish_pct=float(item.get("bearish_pct", 0.0)),
                sample_size=int(item.get("sample_size", 0)),
            )
        )
    return out


def fetch_sentiment(
    codes: Sequence[str],
    *,
    script_path: str | Path = "~/.cursor/skills/futu-comment-sentiment/scripts/run.py",
    timeout: int = 60,
) -> list[SentimentSnapshot]:
    cmd = ["python", str(Path(script_path).expanduser()), "--codes", ",".join(codes)]
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"sentiment script failed: {result.stderr}")
    return _parse(json.loads(result.stdout))
```

- [ ] **Step 3: 测试**

```python
# equity-monitor/tests/unit/test_data_news.py
from datetime import datetime

from equity_monitor.data.news import _parse


def test_parse_news_by_code() -> None:
    payload = {
        "by_code": {
            "US.AAPL": [
                {
                    "ts": "2026-05-02T13:00:00",
                    "source": "Reuters",
                    "title": "AAPL beats Q3 expectations",
                    "url": "https://reuters.com/x",
                    "summary": "Strong iPhone sales drive earnings",
                }
            ]
        }
    }
    out = _parse(payload)
    assert len(out) == 1
    assert out[0].code == "US.AAPL"
    assert out[0].source == "Reuters"
    assert out[0].ts == datetime(2026, 5, 2, 13, 0)
```

```python
# equity-monitor/tests/unit/test_data_sentiment.py
from datetime import datetime

from equity_monitor.data.sentiment import _parse


def test_parse_sentiment() -> None:
    payload = {
        "snapshots": [
            {
                "code": "US.AAPL",
                "ts": "2026-05-02T14:30:00",
                "temperature": 7.2,
                "bullish_pct": 62.5,
                "bearish_pct": 18.0,
                "sample_size": 480,
            }
        ]
    }
    out = _parse(payload)
    assert out[0].temperature == 7.2
    assert out[0].ts == datetime(2026, 5, 2, 14, 30)
```

- [ ] **Step 4: 验证 + commit**

```bash
pytest tests/unit/test_data_news.py tests/unit/test_data_sentiment.py -v
git add src/equity_monitor/data/news.py src/equity_monitor/data/sentiment.py \
        tests/unit/test_data_news.py tests/unit/test_data_sentiment.py
git commit -m "feat(data): Futu news & comment sentiment subprocess wrappers"
```

---

## Task 11: signals.threshold + signals.tech

**Files:**
- Create: `equity-monitor/src/equity_monitor/signals/__init__.py`
- Create: `equity-monitor/src/equity_monitor/signals/base.py`
- Create: `equity-monitor/src/equity_monitor/signals/threshold.py`
- Create: `equity-monitor/src/equity_monitor/signals/tech.py`
- Create: `equity-monitor/tests/unit/test_signals_threshold.py`
- Create: `equity-monitor/tests/unit/test_signals_tech.py`

- [ ] **Step 1: 写 base.py**

```python
# equity-monitor/src/equity_monitor/signals/__init__.py
```

```python
# equity-monitor/src/equity_monitor/signals/base.py
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class Severity(str, Enum):
    INFO = "INFO"
    WARN = "WARN"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True, slots=True)
class Signal:
    code: str
    ts: datetime
    signal_type: str
    severity: Severity
    payload: dict[str, Any] = field(default_factory=dict)
```

- [ ] **Step 2: 写 threshold.py**

```python
# equity-monitor/src/equity_monitor/signals/threshold.py
from __future__ import annotations

from datetime import datetime

from equity_monitor.signals.base import Severity, Signal


def detect_threshold_breach(
    *,
    code: str,
    ts: datetime,
    close: float,
    upper: float | None,
    lower: float | None,
) -> list[Signal]:
    out: list[Signal] = []
    if upper is not None and close >= upper:
        out.append(
            Signal(
                code=code,
                ts=ts,
                signal_type="threshold_breach_upper",
                severity=Severity.CRITICAL,
                payload={"close": close, "upper": upper},
            )
        )
    if lower is not None and close <= lower:
        out.append(
            Signal(
                code=code,
                ts=ts,
                signal_type="threshold_breach_lower",
                severity=Severity.CRITICAL,
                payload={"close": close, "lower": lower},
            )
        )
    return out
```

- [ ] **Step 3: 写 tech.py**

```python
# equity-monitor/src/equity_monitor/signals/tech.py
from __future__ import annotations

from datetime import datetime

import pandas as pd

from equity_monitor.signals.base import Severity, Signal


def detect_tech_signals(
    code: str,
    indicators_df: pd.DataFrame,
    *,
    rsi_overbought: float = 70.0,
    rsi_oversold: float = 30.0,
) -> list[Signal]:
    """Inspect the LATEST row of indicators_df (must include rsi_14, macd_hist, boll_*).

    Detects:
      - rsi_overbought / rsi_oversold (latest row)
      - macd_golden_cross / macd_death_cross (sign flip vs prev row)
      - boll_upper_break / boll_lower_break (latest close vs latest band)
    """
    if len(indicators_df) < 2:
        return []
    last = indicators_df.iloc[-1]
    prev = indicators_df.iloc[-2]
    ts: datetime = indicators_df.index[-1].to_pydatetime() if hasattr(
        indicators_df.index[-1], "to_pydatetime"
    ) else indicators_df.index[-1]

    out: list[Signal] = []

    if pd.notna(last["rsi_14"]):
        if last["rsi_14"] > rsi_overbought:
            out.append(
                Signal(
                    code=code,
                    ts=ts,
                    signal_type="rsi_overbought",
                    severity=Severity.WARN,
                    payload={"rsi": float(last["rsi_14"])},
                )
            )
        if last["rsi_14"] < rsi_oversold:
            out.append(
                Signal(
                    code=code,
                    ts=ts,
                    signal_type="rsi_oversold",
                    severity=Severity.WARN,
                    payload={"rsi": float(last["rsi_14"])},
                )
            )

    if pd.notna(last["macd_hist"]) and pd.notna(prev["macd_hist"]):
        if prev["macd_hist"] <= 0 < last["macd_hist"]:
            out.append(
                Signal(
                    code=code,
                    ts=ts,
                    signal_type="macd_golden_cross",
                    severity=Severity.WARN,
                    payload={"macd_hist": float(last["macd_hist"])},
                )
            )
        if prev["macd_hist"] >= 0 > last["macd_hist"]:
            out.append(
                Signal(
                    code=code,
                    ts=ts,
                    signal_type="macd_death_cross",
                    severity=Severity.WARN,
                    payload={"macd_hist": float(last["macd_hist"])},
                )
            )

    if pd.notna(last["close"]) and pd.notna(last["boll_upper"]):
        if last["close"] > last["boll_upper"]:
            out.append(
                Signal(
                    code=code,
                    ts=ts,
                    signal_type="boll_upper_break",
                    severity=Severity.INFO,
                    payload={"close": float(last["close"]), "upper": float(last["boll_upper"])},
                )
            )
    if pd.notna(last["close"]) and pd.notna(last["boll_lower"]):
        if last["close"] < last["boll_lower"]:
            out.append(
                Signal(
                    code=code,
                    ts=ts,
                    signal_type="boll_lower_break",
                    severity=Severity.INFO,
                    payload={"close": float(last["close"]), "lower": float(last["boll_lower"])},
                )
            )

    return out
```

- [ ] **Step 4: 测试 threshold**

```python
# equity-monitor/tests/unit/test_signals_threshold.py
from datetime import datetime

from equity_monitor.signals.base import Severity
from equity_monitor.signals.threshold import detect_threshold_breach


def test_upper_breach() -> None:
    out = detect_threshold_breach(
        code="US.AAPL", ts=datetime(2026, 5, 2, 14), close=205.0, upper=200.0, lower=165.0
    )
    assert len(out) == 1
    assert out[0].signal_type == "threshold_breach_upper"
    assert out[0].severity is Severity.CRITICAL


def test_lower_breach() -> None:
    out = detect_threshold_breach(
        code="US.AAPL", ts=datetime(2026, 5, 2, 14), close=160.0, upper=200.0, lower=165.0
    )
    assert len(out) == 1
    assert out[0].signal_type == "threshold_breach_lower"


def test_no_breach() -> None:
    out = detect_threshold_breach(
        code="US.AAPL", ts=datetime(2026, 5, 2, 14), close=180.0, upper=200.0, lower=165.0
    )
    assert out == []


def test_thresholds_optional() -> None:
    out = detect_threshold_breach(
        code="US.TSLA", ts=datetime(2026, 5, 2, 14), close=180.0, upper=None, lower=None
    )
    assert out == []
```

- [ ] **Step 5: 测试 tech**

```python
# equity-monitor/tests/unit/test_signals_tech.py
from datetime import datetime

import pandas as pd

from equity_monitor.signals.tech import detect_tech_signals


def _row(rsi=50.0, macd_hist=0.0, close=100.0, lower=80.0, upper=120.0):
    return {
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 1,
        "turnover": 1,
        "rsi_14": rsi,
        "macd": 0.0,
        "macd_signal": 0.0,
        "macd_hist": macd_hist,
        "boll_lower": lower,
        "boll_mid": (lower + upper) / 2,
        "boll_upper": upper,
    }


def _df(rows: list[dict]) -> pd.DataFrame:
    idx = [datetime(2026, 5, 2, 9 + i) for i in range(len(rows))]
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx, name="ts"))


def test_rsi_overbought_emits_signal() -> None:
    df = _df([_row(rsi=50), _row(rsi=72)])
    sigs = detect_tech_signals("US.AAPL", df)
    types = {s.signal_type for s in sigs}
    assert "rsi_overbought" in types


def test_macd_golden_cross() -> None:
    df = _df([_row(macd_hist=-0.2), _row(macd_hist=0.3)])
    sigs = detect_tech_signals("US.AAPL", df)
    assert any(s.signal_type == "macd_golden_cross" for s in sigs)


def test_macd_death_cross() -> None:
    df = _df([_row(macd_hist=0.2), _row(macd_hist=-0.3)])
    sigs = detect_tech_signals("US.AAPL", df)
    assert any(s.signal_type == "macd_death_cross" for s in sigs)


def test_boll_break_upper_info() -> None:
    df = _df([_row(close=100), _row(close=125, upper=120, lower=80)])
    sigs = detect_tech_signals("US.AAPL", df)
    assert any(s.signal_type == "boll_upper_break" for s in sigs)


def test_no_signal_when_normal() -> None:
    df = _df([_row(), _row()])
    sigs = detect_tech_signals("US.AAPL", df)
    assert sigs == []
```

- [ ] **Step 6: 验证 + commit**

```bash
pytest tests/unit/test_signals_threshold.py tests/unit/test_signals_tech.py -v
git add src/equity_monitor/signals/ tests/unit/test_signals_threshold.py tests/unit/test_signals_tech.py
git commit -m "feat(signals): threshold breach and RSI/MACD/Bollinger detectors"
```

---

## Task 12: signals.compose — 合成 / 去重 / 严重度提升

**Files:**
- Create: `equity-monitor/src/equity_monitor/signals/compose.py`
- Create: `equity-monitor/tests/unit/test_compose.py`

- [ ] **Step 1: 写 compose.py**

```python
# equity-monitor/src/equity_monitor/signals/compose.py
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta

from equity_monitor.signals.base import Severity, Signal


REVERSAL_PATTERNS = {"M_top", "W_bottom", "head_and_shoulders", "inverse_head_and_shoulders"}


def upgrade_severity(sig: Signal) -> Signal:
    """Pattern-based severity bump: tech_anomaly + reversal pattern → CRITICAL."""
    if sig.signal_type == "futu_tech_anomaly":
        if sig.payload.get("event") in REVERSAL_PATTERNS:
            return Signal(
                code=sig.code,
                ts=sig.ts,
                signal_type=sig.signal_type,
                severity=Severity.CRITICAL,
                payload=sig.payload,
            )
    return sig


def deduplicate(
    signals: Iterable[Signal],
    *,
    existing_keys: set[tuple[str, str, datetime]] | None = None,
    window_minutes: int = 60,
) -> list[Signal]:
    """Remove duplicates of (code, signal_type) within `window_minutes` window.

    Bucket key uses ts truncated to `window_minutes` slot.
    `existing_keys` lets caller pass keys already in DB to also dedupe across runs.
    """
    seen: set[tuple[str, str, datetime]] = set(existing_keys or set())
    out: list[Signal] = []
    for sig in signals:
        bucket = sig.ts.replace(
            minute=(sig.ts.minute // window_minutes) * window_minutes,
            second=0,
            microsecond=0,
        )
        key = (sig.code, sig.signal_type, bucket)
        if key in seen:
            continue
        seen.add(key)
        out.append(sig)
    return out


def split_by_severity(
    signals: Iterable[Signal],
) -> tuple[list[Signal], list[Signal], list[Signal]]:
    """Return (critical, warn, info)."""
    crit, warn, info = [], [], []
    for s in signals:
        if s.severity is Severity.CRITICAL:
            crit.append(s)
        elif s.severity is Severity.WARN:
            warn.append(s)
        else:
            info.append(s)
    return crit, warn, info


__all__ = [
    "deduplicate",
    "split_by_severity",
    "upgrade_severity",
]


# Reference for implementer: stub kept to suppress "unused" warning if not all
# helpers used downstream yet.
_ = timedelta
```

- [ ] **Step 2: 写测试**

```python
# equity-monitor/tests/unit/test_compose.py
from datetime import datetime

from equity_monitor.signals.base import Severity, Signal
from equity_monitor.signals.compose import (
    deduplicate,
    split_by_severity,
    upgrade_severity,
)


def _s(code, ts, signal_type, sev=Severity.WARN, payload=None) -> Signal:
    return Signal(code=code, ts=ts, signal_type=signal_type, severity=sev, payload=payload or {})


def test_dedupe_same_bucket() -> None:
    a = _s("US.AAPL", datetime(2026, 5, 2, 14, 5), "rsi_overbought")
    b = _s("US.AAPL", datetime(2026, 5, 2, 14, 30), "rsi_overbought")
    c = _s("US.AAPL", datetime(2026, 5, 2, 15, 5), "rsi_overbought")  # next bucket
    out = deduplicate([a, b, c], window_minutes=60)
    assert len(out) == 2
    assert out[0] is a and out[1] is c


def test_dedupe_different_types_kept() -> None:
    a = _s("US.AAPL", datetime(2026, 5, 2, 14, 5), "rsi_overbought")
    b = _s("US.AAPL", datetime(2026, 5, 2, 14, 5), "macd_death_cross")
    out = deduplicate([a, b], window_minutes=60)
    assert len(out) == 2


def test_dedupe_existing_keys_carried_over() -> None:
    a = _s("US.AAPL", datetime(2026, 5, 2, 14, 5), "rsi_overbought")
    existing = {("US.AAPL", "rsi_overbought", datetime(2026, 5, 2, 14, 0))}
    out = deduplicate([a], existing_keys=existing, window_minutes=60)
    assert out == []


def test_split_by_severity() -> None:
    crit = _s("X", datetime(2026, 5, 2, 14), "x", sev=Severity.CRITICAL)
    warn = _s("X", datetime(2026, 5, 2, 14), "y", sev=Severity.WARN)
    info = _s("X", datetime(2026, 5, 2, 14), "z", sev=Severity.INFO)
    c, w, i = split_by_severity([crit, warn, info])
    assert c == [crit] and w == [warn] and i == [info]


def test_upgrade_reversal_pattern_to_critical() -> None:
    s = _s(
        "US.NVDA",
        datetime(2026, 5, 2, 14),
        "futu_tech_anomaly",
        sev=Severity.WARN,
        payload={"event": "M_top", "indicator": "PATTERN"},
    )
    out = upgrade_severity(s)
    assert out.severity is Severity.CRITICAL


def test_upgrade_non_reversal_unchanged() -> None:
    s = _s(
        "US.NVDA",
        datetime(2026, 5, 2, 14),
        "futu_tech_anomaly",
        sev=Severity.WARN,
        payload={"event": "MA_cross", "indicator": "MA"},
    )
    out = upgrade_severity(s)
    assert out.severity is Severity.WARN
```

- [ ] **Step 3: 验证 + commit**

```bash
pytest tests/unit/test_compose.py -v
git add src/equity_monitor/signals/compose.py tests/unit/test_compose.py
git commit -m "feat(signals): compose with dedup, severity split, reversal upgrade"
```

---

## Task 13: scheduler.calendar — NYSE 交易日 + DST

**Files:**
- Create: `equity-monitor/src/equity_monitor/scheduler/__init__.py`
- Create: `equity-monitor/src/equity_monitor/scheduler/calendar.py`
- Create: `equity-monitor/tests/unit/test_calendar.py`

- [ ] **Step 1: 写 calendar.py**

```python
# equity-monitor/src/equity_monitor/scheduler/__init__.py
```

```python
# equity-monitor/src/equity_monitor/scheduler/calendar.py
from __future__ import annotations

from datetime import date, datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal


_NYSE = "NYSE"
_TZ_ET = ZoneInfo("America/New_York")


@lru_cache(maxsize=1)
def _calendar():
    return mcal.get_calendar(_NYSE)


def is_trading_day(d: date) -> bool:
    sched = _calendar().schedule(start_date=d, end_date=d)
    return not sched.empty


def is_market_open_at(when_utc: datetime) -> bool:
    when_et = when_utc.astimezone(_TZ_ET)
    sched = _calendar().schedule(
        start_date=when_et.date(), end_date=when_et.date()
    )
    if sched.empty:
        return False
    open_ts = sched.iloc[0]["market_open"].to_pydatetime()
    close_ts = sched.iloc[0]["market_close"].to_pydatetime()
    return open_ts <= when_utc <= close_ts


def early_close(d: date) -> datetime | None:
    """Return early close datetime (UTC) on shortened sessions, else None."""
    sched = _calendar().schedule(start_date=d, end_date=d)
    if sched.empty:
        return None
    close_ts = sched.iloc[0]["market_close"].to_pydatetime()
    et_close = close_ts.astimezone(_TZ_ET)
    if et_close.hour < 16:  # normal close is 16:00 ET
        return close_ts
    return None
```

- [ ] **Step 2: 测试**

```python
# equity-monitor/tests/unit/test_calendar.py
from datetime import date, datetime, timezone

from equity_monitor.scheduler.calendar import (
    early_close,
    is_market_open_at,
    is_trading_day,
)


def test_weekend_not_trading() -> None:
    # 2026-05-02 is a Saturday
    assert is_trading_day(date(2026, 5, 2)) is False


def test_normal_weekday_is_trading() -> None:
    # 2026-05-04 is a Monday
    assert is_trading_day(date(2026, 5, 4)) is True


def test_christmas_day_not_trading() -> None:
    assert is_trading_day(date(2026, 12, 25)) is False


def test_market_open_during_session() -> None:
    # 2026-05-04 14:00 UTC = 10:00 ET → market open
    when = datetime(2026, 5, 4, 14, 0, tzinfo=timezone.utc)
    assert is_market_open_at(when) is True


def test_market_closed_before_open() -> None:
    # 2026-05-04 12:00 UTC = 08:00 ET → before open
    when = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
    assert is_market_open_at(when) is False


def test_black_friday_early_close() -> None:
    # 2026-11-27 is Black Friday → early close at 13:00 ET
    ec = early_close(date(2026, 11, 27))
    assert ec is not None
```

- [ ] **Step 3: 验证 + commit**

```bash
pytest tests/unit/test_calendar.py -v
git add src/equity_monitor/scheduler/__init__.py src/equity_monitor/scheduler/calendar.py tests/unit/test_calendar.py
git commit -m "feat(scheduler): NYSE trading day + DST + early close helpers"
```

---

## Task 14: reports.card + reports.render — 飞书卡片

**Files:**
- Create: `equity-monitor/src/equity_monitor/reports/__init__.py`
- Create: `equity-monitor/src/equity_monitor/reports/card.py`
- Create: `equity-monitor/src/equity_monitor/reports/render.py`
- Create: `equity-monitor/src/equity_monitor/reports/templates/signal_alert.json.j2`
- Create: `equity-monitor/src/equity_monitor/reports/templates/daily_brief.json.j2`
- Create: `equity-monitor/src/equity_monitor/reports/templates/news_pulse.json.j2`
- Create: `equity-monitor/tests/unit/test_card_render.py`

- [ ] **Step 1: 写 card.py**

```python
# equity-monitor/src/equity_monitor/reports/__init__.py
```

```python
# equity-monitor/src/equity_monitor/reports/card.py
from __future__ import annotations

from equity_monitor.signals.base import Severity


SEVERITY_COLOR = {
    Severity.INFO: "grey",
    Severity.WARN: "orange",
    Severity.CRITICAL: "red",
}


SEVERITY_EMOJI = {
    Severity.INFO: "ℹ️",
    Severity.WARN: "⚠️",
    Severity.CRITICAL: "🔴",
}
```

- [ ] **Step 2: 写模板**

```jinja2
{# equity-monitor/src/equity_monitor/reports/templates/signal_alert.json.j2 #}
{
  "config": {"wide_screen_mode": true},
  "header": {
    "template": "{{ color }}",
    "title": {"tag": "plain_text", "content": "{{ emoji }} {{ code }} · {{ severity }}"}
  },
  "elements": [
    {
      "tag": "div",
      "text": {"tag": "lark_md", "content": "**${{ '%.2f' | format(close) }}**  {{ change_str }}"}
    },
    {"tag": "hr"},
    {
      "tag": "div",
      "text": {"tag": "lark_md", "content": "**触发信号:**\n{{ signals_md }}"}
    }{% if news_md %},
    {"tag": "hr"},
    {
      "tag": "div",
      "text": {"tag": "lark_md", "content": "**关键新闻:**\n{{ news_md }}"}
    }{% endif %},
    {"tag": "note", "elements": [{"tag": "plain_text", "content": "{{ ts_str }}"}]}
  ]
}
```

```jinja2
{# equity-monitor/src/equity_monitor/reports/templates/daily_brief.json.j2 #}
{
  "config": {"wide_screen_mode": true},
  "header": {
    "template": "blue",
    "title": {"tag": "plain_text", "content": "📊 {{ kind }} · {{ date_str }}"}
  },
  "elements": [
    {
      "tag": "div",
      "text": {"tag": "lark_md", "content": "{{ rows_md }}"}
    },
    {"tag": "hr"},
    {
      "tag": "div",
      "text": {"tag": "lark_md", "content": "{{ summary_md }}"}
    }
  ]
}
```

```jinja2
{# equity-monitor/src/equity_monitor/reports/templates/news_pulse.json.j2 #}
{
  "config": {"wide_screen_mode": true},
  "header": {
    "template": "{{ color }}",
    "title": {"tag": "plain_text", "content": "📰 {{ code }} · {{ headline }}"}
  },
  "elements": [
    {
      "tag": "div",
      "text": {"tag": "lark_md", "content": "情绪温度 **{{ temp_now }}/10** (1h 前 {{ temp_prev }}/10)"}
    },
    {
      "tag": "div",
      "text": {"tag": "lark_md", "content": "{{ news_md }}"}
    }
  ]
}
```

- [ ] **Step 3: 写 render.py**

```python
# equity-monitor/src/equity_monitor/reports/render.py
from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from datetime import datetime
from importlib.resources import files
from typing import Any
from zoneinfo import ZoneInfo

from jinja2 import Environment

from equity_monitor.reports.card import SEVERITY_COLOR, SEVERITY_EMOJI
from equity_monitor.signals.base import Severity, Signal


_TZ_ET = ZoneInfo("America/New_York")
_TZ_CN = ZoneInfo("Asia/Shanghai")


def _load_template(name: str) -> str:
    pkg = files("equity_monitor.reports") / "templates"
    return (pkg / name).read_text()


def _env() -> Environment:
    return Environment(autoescape=False)


def _ts_str(ts: datetime) -> str:
    et = ts.astimezone(_TZ_ET).strftime("%Y-%m-%d %H:%M ET")
    cn = ts.astimezone(_TZ_CN).strftime("%Y-%m-%d %H:%M +8")
    return f"{et} ({cn})"


def render_signal_alert(
    *,
    code: str,
    ts: datetime,
    close: float,
    change_pct: float,
    signals: Sequence[Signal],
    news_titles: Sequence[str] = (),
) -> dict[str, Any]:
    severity = max(
        (s.severity for s in signals),
        key=lambda x: ["INFO", "WARN", "CRITICAL"].index(x.value),
        default=Severity.INFO,
    )
    signals_md = "\n".join(f"• {_signal_line(s)}" for s in signals)
    news_md = "\n".join(f"• {t}" for t in news_titles)
    change_str = f"{'▲' if change_pct >= 0 else '▼'} {change_pct:+.2%}"

    tpl = _env().from_string(_load_template("signal_alert.json.j2"))
    rendered = tpl.render(
        code=code,
        severity=severity.value,
        color=SEVERITY_COLOR[severity],
        emoji=SEVERITY_EMOJI[severity],
        close=close,
        change_str=change_str,
        signals_md=signals_md,
        news_md=news_md,
        ts_str=_ts_str(ts),
    )
    return json.loads(rendered)


def _signal_line(s: Signal) -> str:
    name = {
        "rsi_overbought": "RSI 超买",
        "rsi_oversold": "RSI 超卖",
        "macd_golden_cross": "MACD 金叉",
        "macd_death_cross": "MACD 死叉",
        "boll_upper_break": "突破布林上轨",
        "boll_lower_break": "跌破布林下轨",
        "threshold_breach_upper": "穿越上限阈值",
        "threshold_breach_lower": "穿越下限阈值",
        "futu_tech_anomaly": "技术异动",
        "futu_capital_anomaly": "资金异动",
        "news_negative_burst": "负面舆情突增",
        "news_positive_burst": "正面舆情突增",
    }.get(s.signal_type, s.signal_type)
    detail = ", ".join(f"{k}={v}" for k, v in s.payload.items())
    return f"{name} ({detail})" if detail else name


def render_daily_brief(
    *,
    kind: str,                      # "开盘后1h盘点" | "收盘盘点"
    date_str: str,
    rows: Iterable[dict[str, Any]],  # [{code, close, change_pct, signal_count}, ...]
    summary_lines: Sequence[str] = (),
) -> dict[str, Any]:
    rows_md_lines = []
    for r in rows:
        rows_md_lines.append(
            f"[{r['code']}] **${r['close']:.2f}**  {r['change_pct']:+.2%}  信号:{r['signal_count']}"
        )
    rows_md = "\n".join(rows_md_lines)
    summary_md = "\n".join(f"• {line}" for line in summary_lines)

    tpl = _env().from_string(_load_template("daily_brief.json.j2"))
    rendered = tpl.render(kind=kind, date_str=date_str, rows_md=rows_md, summary_md=summary_md)
    return json.loads(rendered)


def render_news_pulse(
    *,
    code: str,
    direction: str,                  # "negative" | "positive"
    temp_now: float,
    temp_prev: float,
    news_titles: Sequence[str],
) -> dict[str, Any]:
    headline = "负面舆情突增" if direction == "negative" else "正面舆情突增"
    color = "red" if direction == "negative" else "green"
    news_md = "\n".join(f"• {t}" for t in news_titles)

    tpl = _env().from_string(_load_template("news_pulse.json.j2"))
    rendered = tpl.render(
        code=code,
        headline=headline,
        color=color,
        temp_now=f"{temp_now:.1f}",
        temp_prev=f"{temp_prev:.1f}",
        news_md=news_md,
    )
    return json.loads(rendered)
```

- [ ] **Step 4: 测试**

```python
# equity-monitor/tests/unit/test_card_render.py
from datetime import datetime, timezone

from equity_monitor.reports.render import (
    render_daily_brief,
    render_news_pulse,
    render_signal_alert,
)
from equity_monitor.signals.base import Severity, Signal


def test_signal_alert_card_structure() -> None:
    sig = Signal(
        code="US.NVDA",
        ts=datetime(2026, 5, 2, 18, 30, tzinfo=timezone.utc),
        signal_type="rsi_oversold",
        severity=Severity.WARN,
        payload={"rsi": 28.4},
    )
    card = render_signal_alert(
        code="US.NVDA",
        ts=datetime(2026, 5, 2, 18, 30, tzinfo=timezone.utc),
        close=135.42,
        change_pct=-0.023,
        signals=[sig],
        news_titles=["NVDA Q3 指引下调"],
    )
    assert card["header"]["template"] == "orange"
    assert "US.NVDA" in card["header"]["title"]["content"]
    body = json._dumps_safely(card) if hasattr(__import__("json"), "_dumps_safely") else None  # noqa
    elements_text = " ".join(
        e.get("text", {}).get("content", "") for e in card["elements"] if isinstance(e, dict)
    )
    assert "RSI 超卖" in elements_text
    assert "rsi=28.4" in elements_text


def test_daily_brief_rows_render() -> None:
    card = render_daily_brief(
        kind="收盘盘点",
        date_str="2026-05-02 (Fri)",
        rows=[
            {"code": "US.NVDA", "close": 135.42, "change_pct": -0.023, "signal_count": 2},
            {"code": "US.AAPL", "close": 182.30, "change_pct": 0.008, "signal_count": 0},
        ],
        summary_lines=["资金异动 Top3: NVDA / AMD / META"],
    )
    elements_text = " ".join(
        e.get("text", {}).get("content", "") for e in card["elements"] if isinstance(e, dict)
    )
    assert "US.NVDA" in elements_text
    assert "US.AAPL" in elements_text


def test_news_pulse_negative() -> None:
    card = render_news_pulse(
        code="US.NVDA",
        direction="negative",
        temp_now=3.2,
        temp_prev=6.8,
        news_titles=["NVDA Q3 指引下调", "分析师下调评级"],
    )
    assert card["header"]["template"] == "red"
    elements_text = " ".join(
        e.get("text", {}).get("content", "") for e in card["elements"] if isinstance(e, dict)
    )
    assert "3.2" in elements_text and "6.8" in elements_text
```

> 测试中删除掉 `json._dumps_safely` 的杂代码，保留断言。

- [ ] **Step 5: 验证 + commit**

```bash
pytest tests/unit/test_card_render.py -v
git add src/equity_monitor/reports/ tests/unit/test_card_render.py
git commit -m "feat(reports): jinja2 Lark Interactive Card renderers (alert/brief/pulse)"
```

---

## Task 15: reports.lark — lark-cli subprocess 推送

**Files:**
- Create: `equity-monitor/src/equity_monitor/reports/lark.py`
- Create: `equity-monitor/tests/unit/test_reports_lark.py`

- [ ] **Step 1: 实现**

```python
# equity-monitor/src/equity_monitor/reports/lark.py
from __future__ import annotations

import json
import subprocess
from typing import Any

from tenacity import retry, stop_after_attempt, wait_exponential


class LarkSendError(RuntimeError):
    pass


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
def send_card(
    card: dict[str, Any],
    *,
    open_id: str,
    receiver_type: str = "chat",
    cli_path: str = "lark-cli",
    timeout: int = 15,
) -> str:
    """Push an Interactive Card via lark-cli. Returns lark message_id.

    NOTE: subcommand layout assumed `lark-cli im +send-card --open-id ... --card '<json>'`.
    Implementer should run `lark-cli im --help` first; if subcommand differs, adjust here.
    """
    payload = json.dumps(card)
    cmd = [
        cli_path,
        "im",
        "+send-card",
        f"--{receiver_type}-open-id",
        open_id,
        "--card",
        payload,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode != 0:
        raise LarkSendError(f"lark-cli failed: {result.stderr}")
    out = result.stdout.strip()
    try:
        parsed = json.loads(out)
        return str(parsed.get("message_id", out))
    except json.JSONDecodeError:
        return out
```

- [ ] **Step 2: 测试（mock subprocess.run）**

```python
# equity-monitor/tests/unit/test_reports_lark.py
import json
from unittest.mock import MagicMock, patch

import pytest

from equity_monitor.reports.lark import LarkSendError, send_card


def test_send_card_success() -> None:
    with patch("equity_monitor.reports.lark.subprocess.run") as run:
        run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"message_id": "om_xxx"}),
            stderr="",
        )
        msg_id = send_card({"foo": "bar"}, open_id="ou_aaa", cli_path="lark-cli")
    assert msg_id == "om_xxx"


def test_send_card_failure_raises_after_retries() -> None:
    with patch("equity_monitor.reports.lark.subprocess.run") as run:
        run.return_value = MagicMock(returncode=1, stdout="", stderr="auth fail")
        with pytest.raises(LarkSendError):
            send_card({"foo": "bar"}, open_id="ou_aaa")
        assert run.call_count == 3   # tenacity 3 attempts
```

- [ ] **Step 3: 验证 + commit**

```bash
pytest tests/unit/test_reports_lark.py -v
git add src/equity_monitor/reports/lark.py tests/unit/test_reports_lark.py
git commit -m "feat(reports): lark-cli subprocess sender with tenacity retry"
```

---

## Task 16: scheduler.jobs — intraday_check

**Files:**
- Create: `equity-monitor/src/equity_monitor/scheduler/jobs.py`
- Create: `equity-monitor/tests/integration/__init__.py`
- Create: `equity-monitor/tests/integration/test_intraday_job.py`

- [ ] **Step 1: 实现 jobs.py（先只 intraday_check 占位 + 完整逻辑）**

```python
# equity-monitor/src/equity_monitor/scheduler/jobs.py
from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import sessionmaker

from equity_monitor.config import AppConfig, WatchlistConfig
from equity_monitor.data.indicators import compute_indicators
from equity_monitor.data.kline import fetch_kline_df
from equity_monitor.data.quotes import sync_snapshots
from equity_monitor.db import session_scope
from equity_monitor.futu_client import FutuClient
from equity_monitor.models import Indicator, Signal as SignalRow, Symbol
from equity_monitor.reports.lark import send_card
from equity_monitor.reports.render import render_signal_alert
from equity_monitor.signals.base import Severity, Signal
from equity_monitor.signals.compose import deduplicate, split_by_severity
from equity_monitor.signals.tech import detect_tech_signals
from equity_monitor.signals.threshold import detect_threshold_breach


log = structlog.get_logger(__name__)


SendCardFn = Callable[[dict[str, Any], str, str], str]


def _default_sender(card: dict[str, Any], open_id: str, receiver_type: str) -> str:
    return send_card(card, open_id=open_id, receiver_type=receiver_type)


def _persist_indicator_row(session, sym_id: int, ts: datetime, row: dict[str, float]) -> None:
    stmt = (
        sqlite_insert(Indicator)
        .values(symbol_id=sym_id, ts=ts, **row)
        .on_conflict_do_update(
            index_elements=["symbol_id", "ts"],
            set_=row,
        )
    )
    session.execute(stmt)


def _persist_signal_rows(session, signals: list[Signal]) -> dict[int, str]:
    """Insert signals; return {row_id: signal_type} for delivered tracking."""
    inserted: dict[int, str] = {}
    for s in signals:
        sym = (
            session.query(Symbol).filter(Symbol.code == s.code).one_or_none()
        )
        if sym is None:
            continue
        stmt = (
            sqlite_insert(SignalRow)
            .values(
                symbol_id=sym.id,
                ts=s.ts,
                signal_type=s.signal_type,
                severity=s.severity.value,
                payload_json=json.dumps(s.payload),
                delivered=False,
            )
            .on_conflict_do_nothing(
                index_elements=["symbol_id", "ts", "signal_type"]
            )
        )
        result = session.execute(stmt)
        if result.inserted_primary_key:
            inserted[result.inserted_primary_key[0]] = s.signal_type
    return inserted


def run_intraday_check(
    *,
    client: FutuClient,
    factory: sessionmaker,
    cfg: AppConfig,
    watchlist: WatchlistConfig,
    now_utc: datetime | None = None,
    send_card_fn: SendCardFn = _default_sender,
) -> dict[str, int]:
    """One pass of intraday_check. Returns {'quotes': N, 'signals': M, 'pushed': P}."""
    now_utc = now_utc or datetime.now(tz=timezone.utc)
    codes = [s.code for s in watchlist.symbols]

    inserted_quotes = sync_snapshots(client, factory, codes=codes)

    all_sigs: list[Signal] = []
    with session_scope(factory) as session:
        sym_by_code = {s.code: s for s in session.query(Symbol).filter(Symbol.code.in_(codes))}

    for sym_cfg in watchlist.symbols:
        df = fetch_kline_df(client, sym_cfg.code, ktype="K_60M", limit=200)
        if df.empty:
            continue
        ind_df = compute_indicators(
            df,
            rsi_period=14,
            macd_fast=cfg.signals.macd_fast,
            macd_slow=cfg.signals.macd_slow,
            macd_signal=cfg.signals.macd_signal,
            boll_period=cfg.signals.bollinger_period,
            boll_std=cfg.signals.bollinger_std,
        )
        last = ind_df.iloc[-1]
        last_ts = ind_df.index[-1].to_pydatetime() if hasattr(
            ind_df.index[-1], "to_pydatetime"
        ) else ind_df.index[-1]

        with session_scope(factory) as session:
            sym = sym_by_code.get(sym_cfg.code)
            if sym is None:
                continue
            _persist_indicator_row(
                session,
                sym.id,
                last_ts,
                {
                    "rsi_14": float(last["rsi_14"]) if last.notna()["rsi_14"] else None,
                    "macd": float(last["macd"]) if last.notna()["macd"] else None,
                    "macd_signal": float(last["macd_signal"]) if last.notna()["macd_signal"] else None,
                    "macd_hist": float(last["macd_hist"]) if last.notna()["macd_hist"] else None,
                    "boll_upper": float(last["boll_upper"]) if last.notna()["boll_upper"] else None,
                    "boll_mid": float(last["boll_mid"]) if last.notna()["boll_mid"] else None,
                    "boll_lower": float(last["boll_lower"]) if last.notna()["boll_lower"] else None,
                },
            )

        sigs = detect_threshold_breach(
            code=sym_cfg.code,
            ts=last_ts,
            close=float(last["close"]),
            upper=sym_cfg.upper_threshold,
            lower=sym_cfg.lower_threshold,
        )
        sigs += detect_tech_signals(
            sym_cfg.code,
            ind_df,
            rsi_overbought=cfg.signals.rsi_overbought,
            rsi_oversold=cfg.signals.rsi_oversold,
        )
        all_sigs.extend(sigs)

    deduped = deduplicate(all_sigs, window_minutes=cfg.signals.dedupe_window_minutes)

    with session_scope(factory) as session:
        ids = _persist_signal_rows(session, deduped)
    log.info("intraday_check.signals", n=len(deduped), persisted=len(ids))

    crit, warn, _info = split_by_severity(deduped)
    pushed = 0
    for sig in crit:
        card = render_signal_alert(
            code=sig.code,
            ts=sig.ts,
            close=float(sig.payload.get("close", 0.0)),
            change_pct=0.0,
            signals=[sig],
        )
        try:
            msg_id = send_card_fn(card, cfg.lark.receiver.open_id, cfg.lark.receiver.type)
            pushed += 1
            log.info("intraday_check.push", code=sig.code, msg_id=msg_id)
        except Exception as e:
            log.error("intraday_check.push_failed", code=sig.code, error=str(e))

    if warn:
        by_code: dict[str, list[Signal]] = {}
        for s in warn:
            by_code.setdefault(s.code, []).append(s)
        for code, sigs in by_code.items():
            close = next(iter(s.payload.get("close", 0.0) for s in sigs if "close" in s.payload), 0.0)
            card = render_signal_alert(
                code=code,
                ts=now_utc,
                close=float(close),
                change_pct=0.0,
                signals=sigs,
            )
            try:
                msg_id = send_card_fn(card, cfg.lark.receiver.open_id, cfg.lark.receiver.type)
                pushed += 1
                log.info("intraday_check.push", code=code, count=len(sigs), msg_id=msg_id)
            except Exception as e:
                log.error("intraday_check.push_failed", code=code, error=str(e))

    return {"quotes": inserted_quotes, "signals": len(deduped), "pushed": pushed}
```

- [ ] **Step 2: 写集成测试（FakeFutuClient + in-memory SQLite + mocked sender）**

```python
# equity-monitor/tests/integration/__init__.py
```

```python
# equity-monitor/tests/integration/test_intraday_job.py
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import sessionmaker

from equity_monitor.config import (
    AppConfig,
    DatabaseConfig,
    JobCron,
    LarkConfig,
    LarkReceiver,
    LoggingConfig,
    OpenDConfig,
    SchedulerConfig,
    SignalsConfig,
    SymbolConfig,
    WatchlistConfig,
)
from equity_monitor.db import session_scope
from equity_monitor.futu_client import Candle, FakeFutuClient, Snapshot
from equity_monitor.models import Indicator, Signal as SignalRow, Symbol
from equity_monitor.scheduler.jobs import run_intraday_check


@pytest.fixture
def app_cfg() -> AppConfig:
    return AppConfig(
        opend=OpenDConfig(),
        database=DatabaseConfig(path=":memory:"),
        scheduler=SchedulerConfig(
            timezone="America/New_York",
            jobs={"intraday_check": JobCron(cron="30 9-15 * * mon-fri")},
        ),
        lark=LarkConfig(receiver=LarkReceiver(type="chat", open_id="ou_test")),
        signals=SignalsConfig(),
        logging=LoggingConfig(),
    )


@pytest.fixture
def watchlist() -> WatchlistConfig:
    return WatchlistConfig(
        symbols=[SymbolConfig(code="US.AAPL", name="Apple", upper_threshold=200.0, lower_threshold=165.0)]
    )


@pytest.mark.integration
def test_intraday_check_smoke(
    factory: sessionmaker,
    fake_futu: FakeFutuClient,
    app_cfg: AppConfig,
    watchlist: WatchlistConfig,
) -> None:
    with session_scope(factory) as s:
        s.add(Symbol(code="US.AAPL", name="Apple", upper_threshold=200.0, lower_threshold=165.0))

    base_ts = datetime(2026, 5, 2, 9, 30)
    candles = [
        Candle(
            code="US.AAPL",
            ts=base_ts + timedelta(hours=h),
            open=180.0 + h * 0.5,
            high=181.0 + h * 0.5,
            low=179.5 + h * 0.5,
            close=180.5 + h * 0.5,
            volume=10_000,
            turnover=1.8e6,
        )
        for h in range(40)
    ]
    fake_futu.set_kline("US.AAPL", "K_60M", candles)
    fake_futu.set_snapshot(
        Snapshot(
            code="US.AAPL",
            last_price=199.5,
            open_price=180.0,
            high_price=200.0,
            low_price=179.0,
            volume=12_000_000,
            turnover=2.184e9,
            update_time=base_ts + timedelta(hours=39),
        )
    )

    sent_cards: list = []

    def fake_sender(card, open_id, receiver_type):  # type: ignore[no-untyped-def]
        sent_cards.append((card, open_id, receiver_type))
        return "om_test"

    out = run_intraday_check(
        client=fake_futu,
        factory=factory,
        cfg=app_cfg,
        watchlist=watchlist,
        send_card_fn=fake_sender,
    )

    assert out["quotes"] == 1
    assert out["signals"] >= 0  # 不强求一定有信号

    with session_scope(factory) as s:
        ind_count = s.query(Indicator).count()
        sig_count = s.query(SignalRow).count()
        assert ind_count == 1
        assert sig_count == out["signals"]
```

- [ ] **Step 3: 验证 + commit**

```bash
pytest tests/integration/test_intraday_job.py -v -m integration
git add src/equity_monitor/scheduler/jobs.py tests/integration/
git commit -m "feat(scheduler): intraday_check job pulling quotes/indicators/signals → Lark"
```

---

## Task 17: scheduler.jobs — morning_brief & closing_brief

**Files:**
- Modify: `equity-monitor/src/equity_monitor/scheduler/jobs.py` (追加两个函数)
- Create: `equity-monitor/tests/integration/test_brief_jobs.py`

- [ ] **Step 1: 追加 brief job 实现**

```python
# 在 jobs.py 末尾追加
from datetime import date as _date

from equity_monitor.reports.render import render_daily_brief


def _aggregate_signal_count(session, sym_id: int, since: datetime) -> int:
    return (
        session.query(SignalRow)
        .filter(SignalRow.symbol_id == sym_id, SignalRow.ts >= since)
        .count()
    )


def run_brief(
    *,
    kind: str,
    client: FutuClient,
    factory: sessionmaker,
    cfg: AppConfig,
    watchlist: WatchlistConfig,
    now_utc: datetime | None = None,
    send_card_fn: SendCardFn = _default_sender,
) -> dict[str, int]:
    now_utc = now_utc or datetime.now(tz=timezone.utc)
    codes = [s.code for s in watchlist.symbols]

    snaps = {s.code: s for s in client.snapshot(codes)}

    rows: list[dict[str, Any]] = []
    summary_lines: list[str] = []
    today_start = datetime.combine(_date.today(), datetime.min.time())

    with session_scope(factory) as session:
        for sc in watchlist.symbols:
            snap = snaps.get(sc.code)
            if snap is None:
                continue
            change_pct = (
                (snap.last_price - snap.open_price) / snap.open_price
                if snap.open_price
                else 0.0
            )
            sym = session.query(Symbol).filter(Symbol.code == sc.code).one_or_none()
            sig_count = _aggregate_signal_count(session, sym.id, today_start) if sym else 0
            rows.append(
                {
                    "code": sc.code,
                    "close": snap.last_price,
                    "change_pct": change_pct,
                    "signal_count": sig_count,
                }
            )

    if rows:
        gainers = sorted(rows, key=lambda r: r["change_pct"], reverse=True)[:3]
        losers = sorted(rows, key=lambda r: r["change_pct"])[:3]
        summary_lines.append(
            "Top 涨: " + ", ".join(f"{r['code']} {r['change_pct']:+.2%}" for r in gainers)
        )
        summary_lines.append(
            "Top 跌: " + ", ".join(f"{r['code']} {r['change_pct']:+.2%}" for r in losers)
        )

    card = render_daily_brief(
        kind=kind,
        date_str=now_utc.strftime("%Y-%m-%d"),
        rows=rows,
        summary_lines=summary_lines,
    )
    pushed = 0
    try:
        msg_id = send_card_fn(card, cfg.lark.receiver.open_id, cfg.lark.receiver.type)
        pushed = 1
        log.info("brief.push", kind=kind, msg_id=msg_id)
    except Exception as e:
        log.error("brief.push_failed", kind=kind, error=str(e))

    return {"rows": len(rows), "pushed": pushed}


def run_morning_brief(**kw) -> dict[str, int]:  # type: ignore[no-untyped-def]
    return run_brief(kind="开盘后1h盘点", **kw)


def run_closing_brief(**kw) -> dict[str, int]:  # type: ignore[no-untyped-def]
    return run_brief(kind="收盘盘点", **kw)
```

- [ ] **Step 2: 测试**

```python
# equity-monitor/tests/integration/test_brief_jobs.py
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.orm import sessionmaker

from equity_monitor.db import session_scope
from equity_monitor.futu_client import FakeFutuClient, Snapshot
from equity_monitor.models import Symbol
from equity_monitor.scheduler.jobs import run_morning_brief


@pytest.mark.integration
def test_morning_brief_pushes_card(
    factory: sessionmaker,
    fake_futu: FakeFutuClient,
    app_cfg,
    watchlist,
) -> None:
    with session_scope(factory) as s:
        s.add(Symbol(code="US.AAPL", name="Apple", upper_threshold=200.0, lower_threshold=165.0))

    fake_futu.set_snapshot(
        Snapshot(
            code="US.AAPL",
            last_price=185.0,
            open_price=180.0,
            high_price=186.0,
            low_price=179.0,
            volume=12_000_000,
            turnover=2.184e9,
            update_time=datetime(2026, 5, 4, 14, 30),
        )
    )

    sent: list = []

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
```

> 该测试复用 T16 conftest 里 `app_cfg`, `watchlist` fixtures。需要把它们提到 `tests/conftest.py` 顶层（不是 integration 子目录），或在 `tests/integration/conftest.py` 里 import。在 T16 末尾应该已经把这两个 fixture 抽到 `tests/conftest.py` 共享；如果还在 `test_intraday_job.py` 内，先抽出来。

- [ ] **Step 3: 抽 fixture 到 tests/conftest.py（如果 T16 还没做）**

把 `app_cfg`, `watchlist` 两个 fixture 从 `tests/integration/test_intraday_job.py` 移到 `tests/conftest.py`，并删除原文件中重复定义。

- [ ] **Step 4: 验证 + commit**

```bash
pytest tests/integration/test_brief_jobs.py -v -m integration
git add src/equity_monitor/scheduler/jobs.py tests/integration/test_brief_jobs.py tests/conftest.py
git commit -m "feat(scheduler): morning/closing brief jobs with Lark daily card"
```

---

## Task 18: scheduler.jobs — news_pulse

**Files:**
- Modify: `equity-monitor/src/equity_monitor/scheduler/jobs.py` (追加 news_pulse + 把 news/sentiment 接进来)
- Create: `equity-monitor/tests/integration/test_news_pulse.py`

- [ ] **Step 1: 追加 news_pulse**

```python
# 在 jobs.py 末尾追加
from equity_monitor.data.news import fetch_news_digest
from equity_monitor.data.sentiment import fetch_sentiment
from equity_monitor.models import NewsDigest
from equity_monitor.reports.render import render_news_pulse


def _persist_news(session, sym_id: int, items) -> int:  # type: ignore[no-untyped-def]
    n = 0
    for it in items:
        stmt = (
            sqlite_insert(NewsDigest)
            .values(
                symbol_id=sym_id,
                ts=it.ts,
                source=it.source,
                title=it.title,
                url=it.url,
                summary=it.summary,
                sentiment_score=None,
            )
            .on_conflict_do_nothing(index_elements=["symbol_id", "url"])
        )
        result = session.execute(stmt)
        if result.rowcount > 0:
            n += 1
    return n


def run_news_pulse(
    *,
    factory: sessionmaker,
    cfg: AppConfig,
    watchlist: WatchlistConfig,
    fetch_news: Callable = fetch_news_digest,
    fetch_sent: Callable = fetch_sentiment,
    sentiment_history: dict[str, float] | None = None,
    send_card_fn: SendCardFn = _default_sender,
) -> dict[str, int]:
    """Pull news + sentiment; push pulse card on burst events."""
    sentiment_history = sentiment_history if sentiment_history is not None else {}
    codes = [s.code for s in watchlist.symbols]

    news_items = fetch_news(codes)
    sent_now = {s.code: s for s in fetch_sent(codes)}

    inserted_news = 0
    pushed = 0
    with session_scope(factory) as session:
        sym_by_code = {s.code: s for s in session.query(Symbol).filter(Symbol.code.in_(codes))}
        by_code: dict[str, list] = {}
        for it in news_items:
            by_code.setdefault(it.code, []).append(it)
        for code, items in by_code.items():
            sym = sym_by_code.get(code)
            if sym is None:
                continue
            inserted_news += _persist_news(session, sym.id, items)

    for code, snap in sent_now.items():
        prev = sentiment_history.get(code)
        if prev is None:
            sentiment_history[code] = snap.temperature
            continue
        delta = snap.temperature - prev
        direction = None
        if delta <= -cfg.signals.news_burst_drop:
            direction = "negative"
        elif delta >= cfg.signals.news_burst_rise:
            direction = "positive"
        if direction:
            titles = [it.title for it in news_items if it.code == code][:3]
            card = render_news_pulse(
                code=code,
                direction=direction,
                temp_now=snap.temperature,
                temp_prev=prev,
                news_titles=titles,
            )
            try:
                msg_id = send_card_fn(card, cfg.lark.receiver.open_id, cfg.lark.receiver.type)
                pushed += 1
                log.info("news_pulse.push", code=code, dir=direction, msg_id=msg_id)
            except Exception as e:
                log.error("news_pulse.push_failed", code=code, error=str(e))
        sentiment_history[code] = snap.temperature

    return {"news_inserted": inserted_news, "pushed": pushed}
```

- [ ] **Step 2: 测试**

```python
# equity-monitor/tests/integration/test_news_pulse.py
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.orm import sessionmaker

from equity_monitor.data.news import NewsItem
from equity_monitor.data.sentiment import SentimentSnapshot
from equity_monitor.db import session_scope
from equity_monitor.models import NewsDigest, Symbol
from equity_monitor.scheduler.jobs import run_news_pulse


@pytest.mark.integration
def test_news_pulse_negative_burst(factory: sessionmaker, app_cfg, watchlist) -> None:
    with session_scope(factory) as s:
        s.add(Symbol(code="US.AAPL", name="Apple", upper_threshold=200.0, lower_threshold=165.0))

    def fake_news(codes):  # type: ignore[no-untyped-def]
        return [
            NewsItem(
                code="US.AAPL",
                ts=datetime(2026, 5, 2, 14, 0),
                source="Reuters",
                title="AAPL guidance miss",
                url="https://r.com/1",
                summary="x",
            )
        ]

    def fake_sent(codes):  # type: ignore[no-untyped-def]
        return [
            SentimentSnapshot(
                code="US.AAPL",
                ts=datetime(2026, 5, 2, 14, 30),
                temperature=3.5,
                bullish_pct=20,
                bearish_pct=70,
                sample_size=400,
            )
        ]

    sent_cards: list = []

    def fake_sender(card, open_id, receiver_type):  # type: ignore[no-untyped-def]
        sent_cards.append(card)
        return "om_test"

    history = {"US.AAPL": 7.0}  # prev temp 7.0 → drop to 3.5 (delta=-3.5 ≥ 3.0)
    out = run_news_pulse(
        factory=factory,
        cfg=app_cfg,
        watchlist=watchlist,
        fetch_news=fake_news,
        fetch_sent=fake_sent,
        sentiment_history=history,
        send_card_fn=fake_sender,
    )

    assert out["pushed"] == 1
    assert out["news_inserted"] == 1
    assert sent_cards[0]["header"]["template"] == "red"

    with session_scope(factory) as s:
        assert s.query(NewsDigest).count() == 1
```

- [ ] **Step 3: 验证 + commit**

```bash
pytest tests/integration/test_news_pulse.py -v -m integration
git add src/equity_monitor/scheduler/jobs.py tests/integration/test_news_pulse.py
git commit -m "feat(scheduler): news_pulse job with sentiment burst detection"
```

---

## Task 19: scheduler.runner — APScheduler 长驻入口

**Files:**
- Create: `equity-monitor/src/equity_monitor/scheduler/runner.py`
- Create: `equity-monitor/tests/unit/test_runner_wiring.py`

- [ ] **Step 1: 写 runner.py**

```python
# equity-monitor/src/equity_monitor/scheduler/runner.py
from __future__ import annotations

import logging
import signal
from collections.abc import Callable
from typing import Any

import structlog
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from equity_monitor.config import AppConfig, WatchlistConfig
from equity_monitor.db import init_schema, make_engine, make_sessionmaker
from equity_monitor.futu_client import FutuClient, OpenDClient
from equity_monitor.scheduler.calendar import is_trading_day
from equity_monitor.scheduler.jobs import (
    run_closing_brief,
    run_intraday_check,
    run_morning_brief,
    run_news_pulse,
)


def _setup_logging(level: str = "INFO", file_path: str | None = None) -> None:
    logging.basicConfig(level=level)
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ]
    )


def _wrap_trading_day(fn: Callable[..., Any]) -> Callable[..., Any]:
    from datetime import datetime, timezone

    log = structlog.get_logger("scheduler.runner")

    def wrapper(*args, **kwargs):  # type: ignore[no-untyped-def]
        from datetime import date

        today = datetime.now(tz=timezone.utc).astimezone().date()
        if not is_trading_day(today):
            log.info("skip.non_trading_day", date=str(today), job=fn.__name__)
            return None
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            log.error("job.failed", job=fn.__name__, error=str(e))

    wrapper.__name__ = fn.__name__
    return wrapper


def build_scheduler(
    *,
    cfg: AppConfig,
    watchlist: WatchlistConfig,
    client_factory: Callable[[], FutuClient] | None = None,
) -> BlockingScheduler:
    sched = BlockingScheduler(timezone=cfg.scheduler.timezone)

    engine = make_engine(cfg.database.path, wal_mode=cfg.database.wal_mode)
    init_schema(engine)
    factory = make_sessionmaker(engine)

    client_factory = client_factory or (
        lambda: OpenDClient(cfg.opend.host, cfg.opend.port)
    )

    sentiment_history: dict[str, float] = {}

    def with_client(job_fn, *, kind: str | None = None):  # type: ignore[no-untyped-def]
        def runner():  # type: ignore[no-untyped-def]
            client = client_factory()
            try:
                kw: dict[str, Any] = dict(client=client, factory=factory, cfg=cfg, watchlist=watchlist)
                if kind:
                    kw["kind"] = kind
                return job_fn(**kw)
            finally:
                client.close()

        runner.__name__ = job_fn.__name__
        return runner

    def news_runner():  # type: ignore[no-untyped-def]
        return run_news_pulse(
            factory=factory,
            cfg=cfg,
            watchlist=watchlist,
            sentiment_history=sentiment_history,
        )

    sched.add_job(
        _wrap_trading_day(with_client(run_intraday_check)),
        CronTrigger.from_crontab(
            cfg.scheduler.jobs["intraday_check"].cron, timezone=cfg.scheduler.timezone
        ),
        id="intraday_check",
        misfire_grace_time=300,
    )
    sched.add_job(
        _wrap_trading_day(with_client(run_morning_brief)),
        CronTrigger.from_crontab(
            cfg.scheduler.jobs["morning_brief"].cron, timezone=cfg.scheduler.timezone
        ),
        id="morning_brief",
        misfire_grace_time=600,
    )
    sched.add_job(
        _wrap_trading_day(with_client(run_closing_brief)),
        CronTrigger.from_crontab(
            cfg.scheduler.jobs["closing_brief"].cron, timezone=cfg.scheduler.timezone
        ),
        id="closing_brief",
        misfire_grace_time=600,
    )
    sched.add_job(
        _wrap_trading_day(news_runner),
        CronTrigger.from_crontab(
            cfg.scheduler.jobs["news_pulse"].cron, timezone=cfg.scheduler.timezone
        ),
        id="news_pulse",
        misfire_grace_time=300,
    )
    return sched


def run_forever(cfg: AppConfig, watchlist: WatchlistConfig) -> None:
    _setup_logging(cfg.logging.level, cfg.logging.file)
    sched = build_scheduler(cfg=cfg, watchlist=watchlist)

    def _shutdown(signum, frame):  # type: ignore[no-untyped-def]
        sched.shutdown(wait=False)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    sched.start()
```

- [ ] **Step 2: 测 wiring（不真启动）**

```python
# equity-monitor/tests/unit/test_runner_wiring.py
from __future__ import annotations

from equity_monitor.futu_client import FakeFutuClient
from equity_monitor.scheduler.runner import build_scheduler


def test_build_scheduler_registers_four_jobs(app_cfg, watchlist) -> None:
    sched = build_scheduler(
        cfg=app_cfg,
        watchlist=watchlist,
        client_factory=lambda: FakeFutuClient(),
    )
    ids = {j.id for j in sched.get_jobs()}
    assert ids == {"intraday_check", "morning_brief", "closing_brief", "news_pulse"}
    sched.shutdown(wait=False)
```

> 注意：`app_cfg` 里 `database.path` 用临时路径而非 `:memory:`，因为 SQLAlchemy `create_engine` 配合 SQLite in-memory 在多 sessionmaker 间状态不共享。改 `app_cfg` fixture 用 `tmp_path / "test.db"`。在 conftest 调整：

```python
# tests/conftest.py 调整 app_cfg fixture（如还用 :memory:）
@pytest.fixture
def app_cfg(tmp_path) -> AppConfig:
    return AppConfig(
        opend=OpenDConfig(),
        database=DatabaseConfig(path=str(tmp_path / "test.db")),
        scheduler=SchedulerConfig(
            timezone="America/New_York",
            jobs={
                "intraday_check": JobCron(cron="30 9-15 * * mon-fri"),
                "morning_brief": JobCron(cron="30 10 * * mon-fri"),
                "closing_brief": JobCron(cron="30 16 * * mon-fri"),
                "news_pulse": JobCron(cron="*/30 9-15 * * mon-fri"),
            },
        ),
        lark=LarkConfig(receiver=LarkReceiver(type="chat", open_id="ou_test")),
        signals=SignalsConfig(),
        logging=LoggingConfig(),
    )
```

- [ ] **Step 3: 验证 + commit**

```bash
pytest tests/unit/test_runner_wiring.py -v
git add src/equity_monitor/scheduler/runner.py tests/unit/test_runner_wiring.py tests/conftest.py
git commit -m "feat(scheduler): blocking APScheduler runner with trading-day guard"
```

---

## Task 20: cli.main — click 子命令

**Files:**
- Create: `equity-monitor/src/equity_monitor/cli/__init__.py`
- Create: `equity-monitor/src/equity_monitor/cli/main.py`
- Create: `equity-monitor/tests/unit/test_cli.py`

- [ ] **Step 1: 写 cli/main.py**

```python
# equity-monitor/src/equity_monitor/cli/__init__.py
```

```python
# equity-monitor/src/equity_monitor/cli/main.py
from __future__ import annotations

from pathlib import Path

import click

from equity_monitor.config import load_settings, load_watchlist
from equity_monitor.db import init_schema, make_engine, make_sessionmaker, session_scope
from equity_monitor.futu_client import OpenDClient
from equity_monitor.models import Symbol
from equity_monitor.scheduler.jobs import (
    run_closing_brief,
    run_intraday_check,
    run_morning_brief,
    run_news_pulse,
)
from equity_monitor.scheduler.runner import run_forever


@click.group()
@click.option(
    "--settings",
    "settings_path",
    default="config/settings.yaml",
    show_default=True,
    type=click.Path(),
)
@click.option(
    "--watchlist",
    "watchlist_path",
    default="config/watchlist.yaml",
    show_default=True,
    type=click.Path(),
)
@click.pass_context
def cli(ctx: click.Context, settings_path: str, watchlist_path: str) -> None:
    """Equity Monitor CLI."""
    ctx.ensure_object(dict)
    ctx.obj["cfg"] = load_settings(settings_path)
    ctx.obj["watchlist"] = load_watchlist(watchlist_path)


@cli.command()
@click.pass_context
def run(ctx: click.Context) -> None:
    """Start the long-running scheduler."""
    run_forever(ctx.obj["cfg"], ctx.obj["watchlist"])


@cli.command()
@click.option(
    "--job",
    type=click.Choice(["intraday", "morning", "closing", "news"]),
    required=True,
)
@click.pass_context
def once(ctx: click.Context, job: str) -> None:
    """Run a single job once and exit."""
    cfg = ctx.obj["cfg"]
    wl = ctx.obj["watchlist"]
    engine = make_engine(cfg.database.path, wal_mode=cfg.database.wal_mode)
    init_schema(engine)
    factory = make_sessionmaker(engine)

    client = OpenDClient(cfg.opend.host, cfg.opend.port)
    try:
        if job == "intraday":
            res = run_intraday_check(client=client, factory=factory, cfg=cfg, watchlist=wl)
        elif job == "morning":
            res = run_morning_brief(client=client, factory=factory, cfg=cfg, watchlist=wl)
        elif job == "closing":
            res = run_closing_brief(client=client, factory=factory, cfg=cfg, watchlist=wl)
        else:
            res = run_news_pulse(factory=factory, cfg=cfg, watchlist=wl)
    finally:
        client.close()
    click.echo(res)


@cli.group()
def watchlist() -> None:
    """Watchlist subcommands."""


@watchlist.command("list")
@click.pass_context
def watchlist_list(ctx: click.Context) -> None:
    cfg = ctx.obj["cfg"]
    engine = make_engine(cfg.database.path, wal_mode=cfg.database.wal_mode)
    init_schema(engine)
    factory = make_sessionmaker(engine)
    with session_scope(factory) as s:
        for sym in s.query(Symbol).filter(Symbol.is_active.is_(True)).all():
            click.echo(f"{sym.code:12s}  upper={sym.upper_threshold}  lower={sym.lower_threshold}")


@watchlist.command("sync")
@click.pass_context
def watchlist_sync(ctx: click.Context) -> None:
    """Sync watchlist.yaml to symbols table (idempotent upsert)."""
    cfg = ctx.obj["cfg"]
    wl = ctx.obj["watchlist"]
    engine = make_engine(cfg.database.path, wal_mode=cfg.database.wal_mode)
    init_schema(engine)
    factory = make_sessionmaker(engine)

    with session_scope(factory) as s:
        for sc in wl.symbols:
            sym = s.query(Symbol).filter(Symbol.code == sc.code).one_or_none()
            if sym is None:
                s.add(
                    Symbol(
                        code=sc.code,
                        name=sc.name,
                        upper_threshold=sc.upper_threshold,
                        lower_threshold=sc.lower_threshold,
                        notes=sc.notes,
                        is_active=True,
                    )
                )
            else:
                sym.name = sc.name
                sym.upper_threshold = sc.upper_threshold
                sym.lower_threshold = sc.lower_threshold
                sym.notes = sc.notes
                sym.is_active = True
    click.echo(f"synced {len(wl.symbols)} symbols")


@cli.group()
def db() -> None:
    """DB subcommands."""


@db.command("init")
@click.pass_context
def db_init(ctx: click.Context) -> None:
    cfg = ctx.obj["cfg"]
    engine = make_engine(cfg.database.path, wal_mode=cfg.database.wal_mode)
    init_schema(engine)
    Path(cfg.database.path).parent.mkdir(parents=True, exist_ok=True)
    click.echo(f"initialized {cfg.database.path}")
```

- [ ] **Step 2: 测试（用 click.testing.CliRunner）**

```python
# equity-monitor/tests/unit/test_cli.py
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from equity_monitor.cli.main import cli


@pytest.fixture
def cli_root(tmp_path: Path) -> Path:
    (tmp_path / "config").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "config" / "settings.yaml").write_text(
        yaml.safe_dump(
            {
                "opend": {"host": "127.0.0.1", "port": 11111},
                "database": {"path": str(tmp_path / "data" / "x.db"), "wal_mode": False},
                "scheduler": {
                    "timezone": "America/New_York",
                    "jobs": {
                        "intraday_check": {"cron": "30 9-15 * * mon-fri"},
                        "morning_brief": {"cron": "30 10 * * mon-fri"},
                        "closing_brief": {"cron": "30 16 * * mon-fri"},
                        "news_pulse": {"cron": "*/30 9-15 * * mon-fri"},
                    },
                },
                "lark": {"cli_path": "lark-cli", "receiver": {"type": "chat", "open_id": "ou_test"}},
                "signals": {},
                "logging": {"level": "INFO"},
            }
        )
    )
    (tmp_path / "config" / "watchlist.yaml").write_text(
        yaml.safe_dump(
            {
                "symbols": [
                    {"code": "US.AAPL", "name": "Apple", "upper_threshold": 200, "lower_threshold": 165},
                ]
            }
        )
    )
    return tmp_path


def test_db_init(cli_root: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--settings",
            str(cli_root / "config" / "settings.yaml"),
            "--watchlist",
            str(cli_root / "config" / "watchlist.yaml"),
            "db",
            "init",
        ],
    )
    assert result.exit_code == 0, result.output
    assert (cli_root / "data" / "x.db").exists()


def test_watchlist_sync_then_list(cli_root: Path) -> None:
    runner = CliRunner()
    base = [
        "--settings",
        str(cli_root / "config" / "settings.yaml"),
        "--watchlist",
        str(cli_root / "config" / "watchlist.yaml"),
    ]
    runner.invoke(cli, base + ["db", "init"])
    r1 = runner.invoke(cli, base + ["watchlist", "sync"])
    assert r1.exit_code == 0, r1.output
    r2 = runner.invoke(cli, base + ["watchlist", "list"])
    assert "US.AAPL" in r2.output
```

- [ ] **Step 3: 验证 + commit**

```bash
pytest tests/unit/test_cli.py -v
git add src/equity_monitor/cli/ tests/unit/test_cli.py
git commit -m "feat(cli): click subcommands run/once/watchlist/db"
```

---

## Task 21: backfill 命令

**Files:**
- Modify: `equity-monitor/src/equity_monitor/cli/main.py` (加 backfill)
- Create: `equity-monitor/src/equity_monitor/data/backfill.py`
- Create: `equity-monitor/tests/unit/test_backfill.py`

- [ ] **Step 1: 实现 backfill 模块**

```python
# equity-monitor/src/equity_monitor/data/backfill.py
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import sessionmaker

from equity_monitor.data.indicators import compute_indicators
from equity_monitor.data.kline import fetch_kline_df
from equity_monitor.db import session_scope
from equity_monitor.futu_client import FutuClient
from equity_monitor.models import Indicator, Quote, Symbol


def backfill_symbol(
    *,
    client: FutuClient,
    factory: sessionmaker,
    code: str,
    days: int,
) -> dict[str, int]:
    """Backfill 60-min OHLC + indicators for a single symbol over ~days days."""
    limit = max(60, days * 7)  # ~7 K_60M bars per US trading day

    df = fetch_kline_df(client, code, ktype="K_60M", limit=limit)
    if df.empty:
        return {"quotes": 0, "indicators": 0}

    ind = compute_indicators(df)

    inserted_q, inserted_i = 0, 0
    with session_scope(factory) as session:
        sym = session.query(Symbol).filter(Symbol.code == code).one_or_none()
        if sym is None:
            return {"quotes": 0, "indicators": 0}
        for ts, row in df.iterrows():
            stmt = (
                sqlite_insert(Quote)
                .values(
                    symbol_id=sym.id,
                    ts=ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=int(row["volume"]),
                    turnover=float(row["turnover"]),
                )
                .on_conflict_do_nothing(index_elements=["symbol_id", "ts"])
            )
            r = session.execute(stmt)
            if r.rowcount > 0:
                inserted_q += 1
        for ts, row in ind.iterrows():
            stmt = (
                sqlite_insert(Indicator)
                .values(
                    symbol_id=sym.id,
                    ts=ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts,
                    rsi_14=float(row["rsi_14"]) if row.notna()["rsi_14"] else None,
                    macd=float(row["macd"]) if row.notna()["macd"] else None,
                    macd_signal=float(row["macd_signal"]) if row.notna()["macd_signal"] else None,
                    macd_hist=float(row["macd_hist"]) if row.notna()["macd_hist"] else None,
                    boll_upper=float(row["boll_upper"]) if row.notna()["boll_upper"] else None,
                    boll_mid=float(row["boll_mid"]) if row.notna()["boll_mid"] else None,
                    boll_lower=float(row["boll_lower"]) if row.notna()["boll_lower"] else None,
                )
                .on_conflict_do_nothing(index_elements=["symbol_id", "ts"])
            )
            r = session.execute(stmt)
            if r.rowcount > 0:
                inserted_i += 1
    return {"quotes": inserted_q, "indicators": inserted_i}


def backfill_all(
    *,
    client: FutuClient,
    factory: sessionmaker,
    codes: Sequence[str],
    days: int,
) -> dict[str, dict[str, int]]:
    return {code: backfill_symbol(client=client, factory=factory, code=code, days=days) for code in codes}
```

- [ ] **Step 2: 在 cli/main.py 加 backfill 子命令**

```python
# 在 cli/main.py 末尾追加
from equity_monitor.data.backfill import backfill_all


@cli.command()
@click.option("--days", default=30, show_default=True, type=int)
@click.pass_context
def backfill(ctx: click.Context, days: int) -> None:
    """Backfill historical 60-min OHLC + indicators for watchlist."""
    cfg = ctx.obj["cfg"]
    wl = ctx.obj["watchlist"]
    engine = make_engine(cfg.database.path, wal_mode=cfg.database.wal_mode)
    init_schema(engine)
    factory = make_sessionmaker(engine)

    client = OpenDClient(cfg.opend.host, cfg.opend.port)
    try:
        out = backfill_all(
            client=client,
            factory=factory,
            codes=[s.code for s in wl.symbols],
            days=days,
        )
    finally:
        client.close()
    for code, stats in out.items():
        click.echo(f"{code}: quotes={stats['quotes']} indicators={stats['indicators']}")
```

- [ ] **Step 3: 测试**

```python
# equity-monitor/tests/unit/test_backfill.py
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import sessionmaker

from equity_monitor.data.backfill import backfill_symbol
from equity_monitor.db import session_scope
from equity_monitor.futu_client import Candle, FakeFutuClient
from equity_monitor.models import Indicator, Quote, Symbol


def test_backfill_symbol_inserts_rows(factory: sessionmaker, fake_futu: FakeFutuClient) -> None:
    with session_scope(factory) as s:
        s.add(Symbol(code="US.AAPL", name="Apple"))

    base = datetime(2026, 4, 1, 9, 30)
    candles = [
        Candle(
            code="US.AAPL",
            ts=base + timedelta(hours=h),
            open=100.0 + h * 0.1,
            high=101.0 + h * 0.1,
            low=99.0 + h * 0.1,
            close=100.5 + h * 0.1,
            volume=10_000,
            turnover=1.0e6,
        )
        for h in range(50)
    ]
    fake_futu.set_kline("US.AAPL", "K_60M", candles)

    out = backfill_symbol(client=fake_futu, factory=factory, code="US.AAPL", days=10)
    assert out["quotes"] == 50
    assert out["indicators"] == 50

    with session_scope(factory) as s:
        assert s.query(Quote).count() == 50
        assert s.query(Indicator).count() == 50

    out2 = backfill_symbol(client=fake_futu, factory=factory, code="US.AAPL", days=10)
    assert out2["quotes"] == 0
    assert out2["indicators"] == 0
```

- [ ] **Step 4: 验证 + commit**

```bash
pytest tests/unit/test_backfill.py -v
git add src/equity_monitor/data/backfill.py src/equity_monitor/cli/main.py tests/unit/test_backfill.py
git commit -m "feat(backfill): backfill_all + CLI subcommand for historical OHLC + indicators"
```

---

## Task 22: 端到端冒烟 + README + 验收清单

**Files:**
- Modify: `equity-monitor/README.md` (完善)
- Create: `equity-monitor/scripts/smoke_e2e.py`
- Modify: `equity-monitor/docs/superpowers/specs/2026-05-02-equity-monitor-design.md` (勾选验收清单)

- [ ] **Step 1: 完善 README**

```markdown
# Equity Monitor

Hourly US-equity monitor with technical signals, news sentiment, and Lark alerts.

## Features

- Pulls quotes / 60-min K-line / RSI / MACD / Bollinger every trading hour (NYSE)
- Subscribes Futu Technical & Capital Anomaly skills for additional signals
- Aggregates Futu News + Comment Sentiment for fundamentals lite
- Detects multi-source signals, dedupes, splits by severity (INFO / WARN / CRITICAL)
- Pushes structured Interactive Cards to Lark IM via lark-cli

## Quickstart

### 1. Install OpenD (one-time)

```bash
bash scripts/install_opend.sh   # follow prompts
python scripts/check_opend.py
```

### 2. Configure

```bash
cp config/watchlist.example.yaml config/watchlist.yaml
# edit config/watchlist.yaml — pick your symbols and price thresholds
# edit config/settings.yaml — set lark.receiver.open_id to your Lark open_id
```

Find your Lark open_id (in Cursor / lark-cli):

```bash
lark-cli contact +me
```

### 3. Initialize DB

```bash
conda create -n fin python=3.11 -y    # one-time
conda activate fin
pip install -e ".[dev]"
equity-monitor db init
equity-monitor watchlist sync
```

### 4. Backfill historical data (optional but recommended)

```bash
equity-monitor backfill --days 30
```

### 5. Run forever

```bash
tmux new -s equity
conda activate fin
equity-monitor run
# Ctrl-B D to detach
```

## CLI Reference

```
equity-monitor run                    Long-running scheduler.
equity-monitor once --job intraday    One-shot a single job (intraday|morning|closing|news).
equity-monitor backfill --days 30     Backfill 60-min OHLC + indicators.
equity-monitor watchlist list         List active symbols.
equity-monitor watchlist sync         Sync yaml → symbols table.
equity-monitor db init                Initialize SQLite schema.
```

## Scheduling

| Job | Cron (ET) |
|---|---|
| intraday_check | `30 9-15 * * mon-fri` |
| morning_brief | `30 10 * * mon-fri` |
| closing_brief | `30 16 * * mon-fri` |
| news_pulse | `*/30 9-15 * * mon-fri` |

NYSE holidays and DST handled automatically by `pandas-market-calendars`.

## Testing

```bash
pytest -v                              # unit + integration
pytest -v -m "not integration"         # unit only
```

## Architecture

See `docs/superpowers/specs/2026-05-02-equity-monitor-design.md`.
```

- [ ] **Step 2: 写端到端冒烟脚本（OpenD 在线时手动跑）**

```python
# equity-monitor/scripts/smoke_e2e.py
"""End-to-end smoke test against real OpenD + lark-cli.

Prerequisites:
  - OpenD running on 127.0.0.1:11111 (logged in)
  - lark-cli on PATH and authed
  - config/watchlist.yaml + config/settings.yaml populated

Run:
python scripts/smoke_e2e.py
"""
from __future__ import annotations

import sys

from equity_monitor.config import load_settings, load_watchlist
from equity_monitor.db import init_schema, make_engine, make_sessionmaker
from equity_monitor.futu_client import OpenDClient
from equity_monitor.scheduler.jobs import (
    run_closing_brief,
    run_intraday_check,
    run_morning_brief,
    run_news_pulse,
)


def main() -> int:
    cfg = load_settings("config/settings.yaml")
    wl = load_watchlist("config/watchlist.yaml")

    engine = make_engine(cfg.database.path, wal_mode=cfg.database.wal_mode)
    init_schema(engine)
    factory = make_sessionmaker(engine)

    client = OpenDClient(cfg.opend.host, cfg.opend.port)
    try:
        print("--- intraday ---", run_intraday_check(client=client, factory=factory, cfg=cfg, watchlist=wl))
        print("--- morning ---", run_morning_brief(client=client, factory=factory, cfg=cfg, watchlist=wl))
        print("--- closing ---", run_closing_brief(client=client, factory=factory, cfg=cfg, watchlist=wl))
        print("--- news ---", run_news_pulse(factory=factory, cfg=cfg, watchlist=wl))
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3: 用户手动跑 + 在飞书侧确认收到四张卡片**

```bash
cd equity-monitor
equity-monitor db init
equity-monitor watchlist sync
equity-monitor backfill --days 7
python scripts/smoke_e2e.py
# 在飞书检查收到 4 条卡片（intraday / morning / closing / news）
```

- [ ] **Step 4: 在 spec §19 验收清单里勾选已完成项**

打开 `docs/superpowers/specs/2026-05-02-equity-monitor-design.md` §19，把已验证的 `- [ ]` 改成 `- [x]`。

- [ ] **Step 5: 跑全部测试一次确认全绿**

```bash
pytest -v
```

Expected: all tests pass; `pytest -m "not integration"` 也全绿。

- [ ] **Step 6: 最终 commit**

```bash
git add README.md scripts/smoke_e2e.py docs/superpowers/specs/2026-05-02-equity-monitor-design.md
git commit -m "docs: README quickstart + e2e smoke script + acceptance checklist"
git tag phase-1-mvp
```

---

## Self-Review Notes (作者侧)

1. **Spec 覆盖 (vs spec §)**:
   - §3 系统架构 → File Inventory + T0–T22 全部对应
   - §4 项目布局 → File Inventory 一致
   - §5 配置 → T2
   - §6 SQLite Schema → T3
   - §7 调度时序 → T13 + T19
   - §8 信号合成 → T11 + T12
   - §9 飞书卡片 → T14 + T15
   - §10 技术栈 → T0 pyproject
   - §11 测试策略 → 各 task 内 TDD 步骤 + integration 标记
   - §12 CLI → T20 + T21
   - §13 错误处理 → T4 tenacity / T15 retry / T19 trading-day guard
   - §16 与 Futu Skill 边界 → T1 (OpenAPI direct) / T8–T10 (subprocess scripts)
   - §19 验收清单 → T22 step 4
2. **Type consistency**: `Severity` 枚举值（INFO/WARN/CRITICAL）在 T11 / T12 / T14 / T16 全部一致；`signal_type` 字符串在 T11 与 T8–T10 dataclass、T14 `_signal_line` mapping 一致。
3. **Placeholder scan**: 唯一标注假设值的是 T15 lark-cli 子命令名（spec §18 已列为风险），实施时由 implementer 用 `lark-cli im --help` 校准。

---

**End of Plan**
