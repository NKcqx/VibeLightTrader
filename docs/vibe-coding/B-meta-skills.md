# Part B · 7 个 meta-skill

7 个动作，**没说 AI 就会出问题**——但说一句就能省一截。每条用项目里的真实场景起头。

---

## 1 · 先要 plan，不要直接动手

> "想加止损功能" → AI 直接改 5 个文件、写测试、commit、push。

vibe coder 不读代码，读的是 plan。每个非 trivial 的任务前都先来一句：

```
先告诉我 plan，不要直接动手。
拆成 3-5 个 commit 的粒度，我说 go 才开始。
```

trivial 的任务（改个数字、加个 print）不必。判断标准：能不能 1 分钟讲清楚改动范围。

---

## 2 · 跨 3 步以上的任务，要 todo 列表

不维护 todo，AI 做完 A 经常忘了 B。维护 todo，随时能看到 AI 在哪一步。

```
把刚才聊的 4 件事整理成 todo list，逐项 in_progress / completed。
```

任务卡住时，看 todo 立刻知道断在哪一项。journal 模块那批就是靠 todo 一眼看出 AI 卡在 metrics 测试上，直接转 subagent 解掉。

---

## 3 · 能并行就并行

```
subagent driven 模式跑这个任务。
```

适合：探索代码库（一个 agent 找 X、另一个找 Y）、写多个独立模块。
不适合：B 必须等 A 完成、两个 agent 改同一个文件。

journal 模块那批 4 个 subagent 同时干（writer / metrics / errors / tests），比单线程快 60%。

---

## 4 · 看到跑偏，30 秒内打断

打断的几次都是同一节奏——AI 已经开始动键盘，方向不对：

> 暂停一下，你怎么又用到 uv 了？我不是说要复用 conda 'fin' 环境吗。（[C2](./appendix-conversations.md#c2--你怎么又用到-uv)）

> 等等，我暂时不关心策略，先把架子搭好。（[C3](./appendix-conversations.md#c3--暂时不关心策略)）

> 停，OpenD 装好了吗？没装好你跑不了。

三个例子都是早期。后期把"conda env `fin`、OpenD 已装、不要用 uv"全写进 system prompt，类似中断少了很多——**vibe coder 的中断越多，提示词就该越长**。

---

## 5 · AI 想猜原因时，让它先看原始材料

> AI："应该是 scope 没开。" → vibe coder 去飞书后台改半天 → 没用 → AI 接着猜下一个。

这是飞书 listener 那次最浪费时间的一段（[C7](./appendix-conversations.md#c7--忘掉飞书收消息的事吧)）。AI 猜了 3 次"是 scope 没开"，每次让用户改后台。改完都不行。

最后让用户跑：

```bash
lark-cli auth scopes --format json
```

看到输出里 `"tokenType": "user"`，才反应过来：之前一直拿 user 视角的 scope 列表去判断 bot 应该有的 scope——根本对不上号。

每次 AI 说"应该是 X"，问一句"**根据什么？**"——答不出来就让它先取证（命令、截图、完整 traceback）。

---

## 6 · 报错把 traceback 完整贴回去

AI 在它沙盒里跑过的命令，本机经常跑不通——cwd 不一样、conda env 不一样、平台不一样、网络不一样。

一次典型卡点：

> `equity-monitor chart US.AAPL --push` 报错——
> `lark-cli: --image: --file must be a relative path within the current directory, got "/Users/.../var/snapshots/US_AAPL_60m_xxx.png"`

AI 测试时用相对路径，没料到从别的目录调命令时变成绝对路径。**只有看到完整错误**才能定位——是 lark-cli 不允许绝对路径。

省事的姿势：

```
我跑同样的命令报错: <粘贴完整 traceback>
cwd: ~/Documents/Code/equity-monitor
conda env: fin
```

一次性给齐，省 3 轮往返。

---

## 7 · 有些功能，叫停比死磕值钱

飞书 listener 这事——目标是让 vibe coder 在飞书发 `/list /add /report` 直接控制系统、不打开 IDE。

| 做了什么 | 结果 |
|---|---|
| 装好 lark-cli | 出向消息通畅 |
| `lark-cli event consume` 长连接 | 连得上 |
| 收事件 | 永远是 0 |
| 调 5 轮：刷 token / 查 scope / 查事件订阅 / polling fallback / 对官方文档 | 全部对得上但事件不来 |

最后那一句（[C7](./appendix-conversations.md#c7--忘掉飞书收消息的事吧)）：

> 忘掉飞书收消息的事吧，你干不好。现在主动触发一次操盘分析。

放弃后半小时，催生了 `equity-monitor analyze` 命令——比飞书 listener 更有用，目前用得最多的就是它。

叫停的几个信号：同一 bug 调 3 轮没进展、AI 开始重复"再试这个看看"、已经在改非项目代码（飞书后台、防火墙、系统包）、修一个 bug 引出 3 个新 bug。

会绕路是 vibe coder 的上限。

---

## 速查

| # | 一句话 | 何时用 |
|---|---|---|
| 1 | 先要 plan | 改动跨多个文件 |
| 2 | 维护 todo | 任务超过 3 步 |
| 3 | subagent | 任务可拆且无强依赖 |
| 4 | 立即打断 | AI 已经开始按错的方向写 |
| 5 | 要原始材料 | AI 开始猜 |
| 6 | 完整 traceback | "你能跑我不能" |
| 7 | 适时叫停 | 3 轮没进展 / 改非项目代码 |

---

## 下一步

- 一个完整项目怎么走过来 → [Part C · 项目是怎么长出来的](./C-evolution-timeline.md)
- 哪些事 AI 真做不了 → [Part E · AI 写不出来的清单](./E-ai-cant-do-this.md)（下批写）
- 真实对话原文 → [附录 · 对话精简版](./appendix-conversations.md)
