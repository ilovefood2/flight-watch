#!/usr/bin/env python3
"""Push flight-price alerts to Telegram.

Reads alerts.json (written by scan.py) and sends one message if there is
anything worth reporting. Stays silent when there is nothing to say, so the
every-30-minutes schedule does not turn into notification spam.

Credentials come from the environment (GitHub Secrets):
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
import html
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ALERTS_PATH = os.environ.get("ALERTS_PATH", "alerts.json")
MAX_LINES = int(os.environ.get("MAX_ALERT_LINES", 8))
RETRIES = int(os.environ.get("TELEGRAM_RETRIES", 4))

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def send(text):
    if not TOKEN or not CHAT_ID:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set - skipping push.")
        print(text)
        return False
    payload = urllib.parse.urlencode(
        {
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
    ).encode()
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, data=payload), timeout=30
            ) as resp:
                ok = json.load(resp).get("ok", False)
                print("telegram ok" if ok else "telegram returned ok=false")
                return ok
        except urllib.error.HTTPError as exc:
            # Safe to surface: the response body carries Telegram's own error
            # description ("chat not found", "Unauthorized", ...) and never
            # the bot token, which only appears in the request URL.
            try:
                detail = exc.read().decode("utf-8", "ignore")[:300]
            except Exception:  # noqa: BLE001
                detail = "(no body)"
            print(f"telegram HTTP {exc.code} {detail}", file=sys.stderr)
            # 4xx means the request itself is wrong (bad token, bad chat id,
            # malformed HTML) - retrying cannot help. 5xx is Telegram having
            # a moment, and usually clears within seconds.
            if exc.code < 500:
                return False
        except Exception as exc:  # noqa: BLE001 - a push error must not fail the job
            print(f"telegram push failed: {type(exc).__name__}", file=sys.stderr)
        if attempt < RETRIES - 1:
            time.sleep(3 * (attempt + 1))
    print("telegram push failed after retries", file=sys.stderr)
    return False


def main():
    try:
        data = json.load(open(ALERTS_PATH))
    except (OSError, json.JSONDecodeError):
        print("no alerts.json - nothing to do.")
        return

    if data.get("blocked"):
        if data.get("soft_blocked") and not data.get("hard_blocked"):
            detail = (
                f"请求都返回 200，但已知有航班的日期只有 {data.get('canary')} 出了价——"
                "Google 在给 GitHub 的 IP 发空页面（限流）。"
            )
        else:
            detail = f"{data.get('failures')}/{data.get('attempts')} 次请求直接失败。"
        send(
            "⚠️ <b>机票监控抓取异常</b>\n\n"
            + detail
            + "\n\n这轮价格数据不可信。<b>没有推送 ≠ 没有便宜票。</b>"
        )
        return

    alerts = data.get("alerts", [])
    if not alerts:
        print("no alerts - staying quiet.")
        return

    best = alerts[0]
    lines = [
        "✈️ <b>便宜票提醒</b>  YYZ → 上海浦东",
        f"<b>{best['cabin_zh']} CA${best['price']:,}</b>  "
        f"{best['dep'][5:]} → {best['ret'][5:]}（{best['days']}天）",
        "",
    ]
    for a in alerts[:MAX_LINES]:
        drop = (a["prev"] - a["price"]) if a.get("prev") else 0
        delta = f"  ↓{drop:,}" if drop > 0 else ""
        lines.append(
            f"• {a['cabin_zh']} <b>CA${a['price']:,}</b>{delta}　"
            f"{a['dep'][5:]} → {a['ret'][5:]}（{a['days']}天）\n"
            f"　<a href=\"{html.escape(a['url'], quote=True)}\">在 Google Flights 打开</a>"
            f"　<i>{html.escape(a['why'])}</i>"
        )
    if len(alerts) > MAX_LINES:
        lines.append(f"\n…另有 {len(alerts) - MAX_LINES} 个也符合条件")

    lines.append("\n<i>直飞 AC27/28 · 往返含税 · 1 成人 · CAD</i>")
    send("\n".join(lines))


if __name__ == "__main__":
    main()
