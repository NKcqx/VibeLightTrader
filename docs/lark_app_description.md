# 飞书 App / Bot 描述文案

按字段长度提供三档，从 app 后台对应字段直接复制粘贴即可。

---

## 1. 极短版（30 字内）— 适合「应用名称下方的简介」/「机器人名片标语」

```
美股监控 · 实时报价 · 技术指标信号 · 飞书指令操控
```

---

## 2. 短版（~120 字）— 适合「应用描述」字段

```
📈 美股 hourly 监控 + 信号告警机器人。

实时拉取报价、60 分钟 K 线，计算 RSI/MACD/BOLL 三件套指标，价格穿越阈值或出现金叉/死叉等异常时主动推送飞书卡片；支持按需 `/chart` K 线 PNG 快照。支持模拟交易建议、纸面 P&L 跟踪。

直接 DM 我即可增删监控标的，发「帮助」查看指令清单。
```

---

## 3. 完整版（~500 字）— 适合「详细介绍」字段或首次问候

```
📊 equity-monitor — 美股监控 + 模拟交易助手

【核心能力】
• 每小时自动拉取美股报价、K 线、资金流，写入本地数据库
• 自实现 RSI(14) / MACD(12,26,9) / BOLL(20,2σ) 三件套技术指标
• 价格突破阈值、金叉/死叉、布林带破位 → 推送 CRITICAL 信号卡
• 开盘 1h 后 + 收盘后两次 brief 卡片，含三件套数值 + 中文解读 + 收益率 + 持仓 P&L
• 卡片含「建议动作」(BUY/SELL N 股) — CLI 一行命令确认即下模拟单
• 模拟账户接 Futu OpenD（acc_id=19145941，$1M 起始资金）

• 按需 `/chart` / `图` 生成 K 线 PNG（告警成功推卡后亦可自动附图，视部署配置）

【飞书指令】(直接 DM 我即可，无需 @)

📋 列表 / list / ls
   查看当前所有监控标的 + 实时价 + 指标解读

➕ 添加 US.AAPL 上限200 下限165
   /add US.NVDA upper=180 lower=110
   监控 TSLA  (无阈值，仅追踪)
   别名：添加 / 增加 / 监控 / 关注 / /add

🎯 阈值 US.AAPL 上限290 下限200
   /threshold AAPL upper=290 lower=200
   别名：阈值 / 修改 / 更新 / /threshold

📈 /chart <标的> [周期]
   `/chart US.AAPL` （默认 60m）· `/chart AAPL D` · `图 TSLA`
   约 200 根 K·近 30 天纸面 BUY/SELL 标记·开仓成本橙色虚线·现价钢蓝虚线
   周期：5m / 15m / 30m / 60m / D（日）/ W（周）；1m 不支持

🗑 删除 US.AAPL / 取消 AAPL / /remove AAPL
   别名：删除 / 取消 / 停止 / 不监控 / /remove

❓ 帮助 / /help
   完整指令清单

【代码格式】
• US.AAPL / HK.0700 / 裸 ticker AAPL 自动加 US. 前缀
• 阈值关键词：上限/下限、阻力位/支撑位、upper/lower、ub/lb 都识别
• 中文自然语言、英文关键字、/ slash 命令三种风格通用

【数据来源】Futu OpenD (本地 11111 端口) — 实时报价 / K 线 / 模拟交易 API
```

### `/chart <code> [freq]` — K-line snapshot（K 线 PNG）

按需渲染标的 `<code>` 的约 **200-bar** K 线图并作为 PNG 发出，图层包括：

- 近 **30** 天内纸面 **BUY / SELL** 成交标记
- 持仓成本（若有开仓）橙色虚线
- 当前价钢蓝色虚线

**Frequencies**: `5m`, `15m`, `30m`, `60m`（默认）, `D`（日线）, `W`（周线）。

**Examples**:

- `/chart US.AAPL` — 默认 60m
- `/chart AAPL D` — 日线（裸代码自动转为 `US.`）
- `图 TSLA` — 中文别名触发同一路径

**Failure modes**:

- **未知频率**（如 `bogus`、`1m`）→ **静默**（不回复）。
- OpenD **不可达或未启用图片发送** → 文案：「⚠️ /chart 当前不可用 (OpenD 未连接或未启用图片发送)。」
- **渲染/K 线拉取失败** → 「⚠️ /chart 失败: {error}」

---

## 4. CLI 命令（运维参考，可放 README 或操作手册）

```
# Daemon
equity-monitor run                        # 调度器：4 个 cron jobs（intraday/morning/closing/news）
equity-monitor listen                     # 飞书消息监听 + DM 指令分发
equity-monitor listen --rich-cards        # (默认) 回复带实时数据的 Lark 卡片
equity-monitor listen --text-only         # 退化为纯文本回复
equity-monitor listen --backend polling   # (默认，自适应 3s 活跃 / 10s 空闲)
equity-monitor listen --backend websocket # 需 app 后台开启 im.message.receive_v1
equity-monitor listen --poll-interval 15  # 调整空闲 polling 间隔

# 单次运行
equity-monitor once --job intraday        # 信号检查 + 推送
equity-monitor once --job morning         # 开盘后 1h brief
equity-monitor once --job closing         # 收盘 brief
equity-monitor once --job news            # 新闻情绪 pulse

# 数据
equity-monitor backfill --days 30         # 回填历史 K 线 + 指标
equity-monitor watchlist list             # DB 中标的清单
equity-monitor watchlist sync             # config/watchlist.yaml → DB
equity-monitor db init                    # 建表
equity-monitor db status                  # 各表行数

# 模拟交易
equity-monitor trade list [--status pending]      # 待确认建议
equity-monitor trade confirm SIGNAL_ID [--qty N]  # 下单确认
equity-monitor trade cancel SIGNAL_ID             # 取消建议
equity-monitor trade positions                    # 当前持仓
equity-monitor trade pnl [--days N]               # 已实现 P&L

# K 线与快照（Phase 3）
equity-monitor chart US.AAPL [--freq 60m] [--out-dir var/snapshots] [--push]  # 终端渲染 PNG，可加推送到 Lark
```
