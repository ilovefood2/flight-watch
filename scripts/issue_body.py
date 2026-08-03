#!/usr/bin/env python3
"""Render alerts.json as GitHub-issue markdown.

Used as the fallback notification path: if the Telegram push fails, the
workflow pipes this into `gh issue create` so an alert is never lost. GitHub
emails the repo owner on new issues, which is the whole point.
"""
import json
import os
import sys

ALERTS_PATH = os.environ.get("ALERTS_PATH", "alerts.json")

try:
    data = json.load(open(ALERTS_PATH))
except (OSError, json.JSONDecodeError):
    print("(no alerts.json)")
    sys.exit(0)

if data.get("blocked"):
    print("## ⚠️ 抓取异常\n")
    if data.get("soft_blocked") and not data.get("hard_blocked"):
        print(
            f"请求都返回 200，但已知有航班的日期只有 `{data.get('canary')}` 出了价 — "
            "Google 在给 GitHub 的 IP 发空页面（限流）。\n"
        )
    else:
        print(f"`{data.get('failures')}/{data.get('attempts')}` 次请求直接失败。\n")
    print("这轮价格数据不可信。**没有推送 ≠ 没有便宜票。**")
    sys.exit(0)

alerts = data.get("alerts", [])
if not alerts:
    print("(no alerts)")
    sys.exit(0)

best = alerts[0]
print(f"**{best['cabin_zh']} CA${best['price']:,}** — "
      f"{best['dep']} → {best['ret']}（{best['days']} 天）\n")
print("| 舱位 | 价格 | 变化 | 出发 | 返回 | 天数 | 触发原因 | 东航 |")
print("|---|---|---|---|---|---|---|---|")
for a in alerts:
    drop = (a["prev"] - a["price"]) if a.get("prev") else 0
    delta = f"↓{drop:,}" if drop > 0 else "—"
    print(
        f"| {a['cabin_zh']} | [CA${a['price']:,}]({a['url']}) | {delta} | "
        f"{a['dep']} | {a['ret']} | {a['days']} | {a['why']} | "
        f"{'⚠️ 同日有直飞，Google 未报价' if a.get('mu_unpriced') else '—'} |"
    )
print("\n价格仅为 Air Canada AC27/28 · 往返含税 · 1 成人 · CAD")
print("\n> Google 不在服务端给东航报价，标 ⚠️ 的日期东航也飞直飞且可能更便宜。")
print("\n> 这是 Telegram 推送失败后的兜底通知。")
