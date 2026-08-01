# flight-watch — YYZ ⇄ 上海浦东 直飞票价监控

每 30 分钟扫一次 Air Canada **AC27/AC28**（多伦多 ⇄ 上海浦东，波音 787-9 直飞）的
**超经**和**商务**往返票价，发现便宜票就推 Telegram。

不需要任何 API key：脚本自己拼 Google Flights 的 `tfs` protobuf 搜索参数，
解析服务端渲染的 HTML，从 `aria-label="<n> Canadian dollars"` 读价格。

## 触发条件

推送发生在满足任一条件时：

| 条件 | 默认值 |
|---|---|
| 超经 ≤ | CA$3,000 |
| 商务 ≤ | CA$6,200 |
| 比上次扫描跌了 ≥ | CA$150 |

没有符合条件的就**完全不推送**——每半小时一次的频率下，静默很重要。

改阈值：编辑 `.github/workflows/flight-watch.yml` 里的 `ALERT_PREMIUM` /
`ALERT_BUSINESS` / `ALERT_DROP`，或者在 Actions 页面用 **Run workflow** 手动传参。

## 扫描范围

- 出发窗口：`2026-09-01` → `2026-12-20`
- 行程长度：14 天和 17 天（上限 20 天）
- 只扫 AC 实际有航班的星期几（夏季 一/三/五/日，冬季 二/三/四/六，10-25 换季）

全窗口约 128 个日期组合。每次运行只扫 **1/6 的轮转切片 + 固定 watchlist**，
所以 3 小时覆盖一遍全部日期，同时把单次请求量压在 ~54 个。

`scripts/scan.py` 顶部的 `WATCHLIST` 是每次都查的重点日期，可以随时改。

## 配置

需要两个 GitHub Secret：

| Secret | 怎么拿 |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Telegram 里找 [@BotFather](https://t.me/BotFather) → `/newbot` |
| `TELEGRAM_CHAT_ID` | 给你的 bot 发一句话，然后打开 `https://api.telegram.org/bot<TOKEN>/getUpdates`，取 `result[0].message.chat.id` |

设置（**在你自己的终端跑，不要把 token 贴给别人**）：

```bash
gh secret set TELEGRAM_BOT_TOKEN
gh secret set TELEGRAM_CHAT_ID
```

## 本地跑

```bash
WINDOW_START=2026-12-01 WINDOW_END=2026-12-10 STATE_PATH=/tmp/s.json python3 scripts/scan.py
```

没设 Telegram 密钥时，`notify.py` 会把消息打到 stdout 而不是推送，方便调试。

## 文件

```
scripts/scan.py                     抓价 + 比对 + 生成 alerts.json
scripts/notify.py                   推 Telegram
state/prices.json                   价格历史（每次运行自动 commit）
.github/workflows/flight-watch.yml  定时任务
```

## 已知限制

- **GitHub 的定时任务不准时。** `*/30` 在高负载时经常延迟 10–60 分钟，
  官方文档明确说了不保证。把它当"大约每半到一小时"看。
- **可能被 Google 拦。** GitHub runner 用的是 Azure 数据中心 IP，
  高频抓取有被限流的风险。脚本会统计失败率，超过 80% 就判定为被拦并推一条警告，
  而不是假装"没有便宜票"。真被拦了就把 `SLICES` 调大、或把 cron 改成 `0 */2 * * *`。
- **只看 Google Flights 的价。** Trip.com 在某些日期便宜 700–900，但它没有稳定的
  服务端接口可抓。收到提醒后建议再手动比一下 Trip.com。
- 价格是 1 成人、往返含税、只看直飞。
