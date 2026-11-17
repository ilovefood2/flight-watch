# flight-watch — YYZ ⇄ 上海浦东 直飞票价监控（2027）

扫 Air Canada 多伦多 ⇄ 上海浦东**直飞**的**超经**和**商务**往返票价，便宜了就推 Telegram。

**两个方向都扫**：多伦多出发的往返（YYZ→PVG→YYZ）和上海出发的往返（PVG→YYZ→PVG）。
这是两个独立定价的市场，差价可以很大 —— 2027-02-10 同一天，商务舱从上海出发
CA$7,799，从多伦多出发 CA$10,558，**差 2,759**。

不需要 API key：脚本自己拼 Google Flights 的 `tfs` protobuf 搜索参数，解析服务端
渲染的 HTML，从 `aria-label="<n> Canadian dollars"` 读价格。

## 触发条件

| 条件 | 默认 |
|---|---|
| 超经 ≤ | CA$3,200 |
| 商务 ≤ | CA$5,000 |
| 跌 ≥ CA$150 **且** 超经 ≤ CA$5,000 / 商务 ≤ CA$9,000 | — |

前两条是「到你的目标价了」，第三条是「它在动，值得看一眼」。两者的上限故意分开：
2027 年的票价开盘就远高于目标价（1–9 月抽样里商务最低 CA$7,949），如果降价提醒也
按目标价卡，监控会**完全哑掉**。

没有命中就**完全不推送**。

## 扫描范围

- 窗口：`2027-01-01` → `2027-12-31`，**自动裁剪到可售期**
- 行程长度：14 / 21 / 28 天，上限 **20 个工作日**（周一至五、含首尾）
- 两个方向、两个舱位

**可售期（booking horizon）**：航司只开售约 355 天内的航班。脚本每天二分查找一次
最远可售日期，只扫到那里为止 —— 否则一年里有小半年是在空扫。2026-11-16 实测可售
到 **2027-09-27**；这条线每天自己往前走，10 月之后的日期会随着开售被自动纳入。

**不再硬编码星期几。** 2026 年 AC 每周飞 4 班，2027 年 7 月看起来天天飞。写死的
班期表失效时会以最糟糕的方式失败 —— **静默跳过本来有便宜票的日期**。现在全扫，
没航班的日期自然返回空。

## 部署到树莓派

```bash
ssh <user>@10.0.0.33
git clone https://github.com/ilovefood2/flight-watch.git ~/flight-watch
bash ~/flight-watch/deploy/install.sh     # 第一次会生成 .env
nano ~/flight-watch/.env                  # 填 Telegram token / chat id
bash ~/flight-watch/deploy/install.sh     # 再跑一次装 systemd timer
```

装好之后：

```bash
systemctl --user list-timers flight-watch.timer   # 下次运行时间
systemctl --user start flight-watch.service       # 立刻跑一次
journalctl --user -u flight-watch.service -f      # 看日志
```

配置都在 `~/flight-watch/.env`（`chmod 600`，不进 git）。

**为什么用树莓派而不是 GitHub Actions**：
1. GitHub 的定时任务实测平均 **96 分钟**才跑一次（cron 写的是 30 分钟），最慢 223 分钟
2. GitHub runner 用 Azure 数据中心 IP，**实测会被 Google 软封**（返回 200 但页面无价格）
3. 树莓派是住宅 IP + 真 cron，两个问题都没有

## 已知限制

- **看不见东航。** Google 的服务端 HTML 对东航只写 `Total price is unavailable`，
  真实价格是浏览器端 JS 补的。脚本检测到这个标记时会在提醒里标 ⚠️，但拿不到数字。
  2026-09-02 实测 AC 12,188、东航 7,090 —— **这个盲区恰好在 AC 最贵时最严重**。
- **只看 Google 的价。** Trip.com 在某些日期便宜 700–900。收到提醒后建议再比一下。
- 价格是 1 成人、往返含税、只看直飞。

## 文件

```
scripts/scan.py                     抓价 + 比对 + 生成 alerts.json
scripts/notify.py                   推 Telegram
scripts/issue_body.py               Telegram 挂了时的 GitHub Issue 兜底
deploy/install.sh                   树莓派一键安装
deploy/flight-watch.{service,timer} systemd 单元
state/prices.json                   价格历史
```
