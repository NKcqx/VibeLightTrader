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
