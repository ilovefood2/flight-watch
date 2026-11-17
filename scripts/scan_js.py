#!/usr/bin/env python3
"""Scan Trip.com with a real browser for nonstop YYZ<->PEK/PVG fares.

Why this exists alongside scan.py:

Google Flights is fast to scrape but only ever prices Air Canada on these
routes. Air China is marked "Price unavailable" even in a fully rendered
browser, and China Eastern is unpriced on every 2027 date tested - so Google
cannot see Toronto-Beijing at all, and misses the cheaper carrier on
Toronto-Shanghai. Measured 2027-02-10 business: Google 10,558 (AC only),
Trip.com 6,386.

Trip.com renders its prices client-side, so this needs a real browser. That
makes it ~40s per query instead of ~2s, which is why it runs on its own slow
timer over a rotating slice rather than on the 30-minute scan.

Writes the same alert schema as scan.py, so notify.py handles both.
"""
import json
import os
import random
import re
import shutil
import sys
import time
from datetime import date, datetime, timedelta

from playwright.sync_api import sync_playwright

# Trip.com city codes: yto=Toronto, bjs=Beijing(all), sha=Shanghai(all)
ROUTES = [tuple(r.split("-")) for r in
          os.environ.get("JS_ROUTES", "yto-bjs,yto-sha,bjs-yto,sha-yto").split(",")]
CITY_ZH = {"yto": "多伦多", "bjs": "北京", "sha": "上海"}
# Trip.com cabin codes
CABINS = {"premium": "s", "business": "c"}
CABIN_ZH = {"premium": "超经", "business": "商务"}

STATE_PATH = os.environ.get("JS_STATE_PATH", "state/prices_js.json")
ALERTS_PATH = os.environ.get("ALERTS_PATH", "alerts.json")

THRESHOLDS = {
    "premium": int(os.environ.get("ALERT_PREMIUM", 3200)),
    "business": int(os.environ.get("ALERT_BUSINESS", 5000)),
}
DROP_DELTA = int(os.environ.get("ALERT_DROP", 150))
DROP_CEILING = {
    "premium": int(os.environ.get("DROP_CEILING_PREMIUM", 5000)),
    "business": int(os.environ.get("DROP_CEILING_BUSINESS", 9000)),
}

WINDOW_START = os.environ.get("WINDOW_START", "2027-01-01")
WINDOW_END = os.environ.get("WINDOW_END", "2027-12-31")
TRIP_LENGTHS = [int(x) for x in os.environ.get("TRIP_LENGTHS", "14,21,28").split(",")]
MAX_WEEKDAYS = int(os.environ.get("MAX_WEEKDAYS", 20))
# A browser query costs ~40s, so each run takes a thin slice of the window.
PER_RUN = int(os.environ.get("JS_PER_RUN", 16))
NAV_TIMEOUT = int(os.environ.get("JS_NAV_TIMEOUT", 120)) * 1000
SETTLE_MAX = int(os.environ.get("JS_SETTLE_MAX", 70))

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/156.0.0.0 Safari/537.36")
PRICE_RE = re.compile(r"CAD\s?([\d,]{4,9})")
CHEAPEST_RE = re.compile(r"Cheapest\s*\n\s*CAD\s?([\d,]{4,9})")
NO_SERVICE_RE = re.compile(
    r"no available flights found|No direct flights found", re.I)
AIRLINE_RE = re.compile(
    r"(Air Canada|Air China|China Eastern(?: Airlines)?|China Southern(?: Airlines)?|"
    r"Hainan Airlines|Xiamen Air)")


def url(dc, ac, dep, ret, cls):
    return (f"https://ca.trip.com/flights/showfarefirst?dcity={dc}&acity={ac}"
            f"&ddate={dep}&rdate={ret}&triptype=rt&class={cls}&quantity=1"
            f"&nonstoponly=on&locale=en-CA&curr=CAD")


def weekdays_between(d1, d2):
    days = (d2 - d1).days + 1
    full_weeks, rem = divmod(days, 7)
    count = full_weeks * 5
    for i in range(rem):
        if (d1 + timedelta(days=full_weeks * 7 + i)).weekday() < 5:
            count += 1
    return count


def build_targets():
    start = max(date.fromisoformat(WINDOW_START), date.today() + timedelta(days=1))
    end = date.fromisoformat(WINDOW_END)
    full = []
    d = start
    while d <= end:
        for n in TRIP_LENGTHS:
            ret = d + timedelta(days=n)
            if weekdays_between(d, ret) <= MAX_WEEKDAYS:
                for dc, ac in ROUTES:
                    full.append((dc, ac, d.isoformat(), ret.isoformat()))
        d += timedelta(days=1)
    if not full:
        return []
    # Walk the window a slice at a time; the offset rides the clock so
    # consecutive runs pick up where the last one left off.
    slices = max(1, (len(full) + PER_RUN - 1) // PER_RUN)
    off = (int(time.time() // 3600) % slices) * PER_RUN
    targets = full[off:off + PER_RUN]
    print(f"{len(full)} itineraries in window; this run does "
          f"{len(targets)} (offset {off})")
    return targets


def scrape(page, u):
    """(cheapest CAD, airlines seen) for one search, or (None, []).

    Trip.com paints the page shell long before the fares arrive, so poll for
    the price text rather than trusting a fixed sleep - the Pi is slow enough
    that a fixed wait either truncates results or wastes most of a minute.
    """
    page.goto(u, timeout=NAV_TIMEOUT, wait_until="domcontentloaded")
    seg = ""
    for _ in range(SETTLE_MAX // 5):
        page.wait_for_timeout(5000)
        body = page.inner_text("body")
        idx = body.find("Departures to")
        if idx < 0:
            continue
        seg = body[idx:idx + 1500]
        # Poll the RESULTS SECTION, not the whole page: Trip.com paints
        # prices into the footer and promos long before the fare list
        # arrives, so a body-wide check returns an empty segment.
        if PRICE_RE.search(seg):
            break
    if not seg:
        return None, [], "timeout"
    # "no nonstop on this route/date" is a real answer, not a scrape failure.
    # Conflating the two made a run of genuinely serviceless dates look like
    # the scraper had been blocked.
    if NO_SERVICE_RE.search(seg):
        return None, [], "no-nonstop"
    m = CHEAPEST_RE.search(seg) or PRICE_RE.search(seg)
    if not m:
        return None, [], "timeout"
    return (int(m.group(1).replace(",", "")),
            sorted(set(AIRLINE_RE.findall(seg))), "ok")


def main():
    old = {}
    if os.path.exists(STATE_PATH):
        try:
            old = json.load(open(STATE_PATH)).get("prices", {})
        except (json.JSONDecodeError, OSError):
            old = {}

    targets = build_targets()
    new, alerts = dict(old), []
    attempts = misses = no_service = 0

    exe = shutil.which("chromium") or shutil.which("chromium-browser")
    if not exe:
        print("no chromium found", file=sys.stderr)
        return 1

    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=exe, headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
        ctx = browser.new_context(locale="en-CA", user_agent=UA,
                                  viewport={"width": 1400, "height": 1200})
        try:
            for dc, ac, dep, ret in targets:
                days = (date.fromisoformat(ret) - date.fromisoformat(dep)).days
                for cabin, cls in CABINS.items():
                    attempts += 1
                    page = ctx.new_page()
                    try:
                        price, airlines, status = scrape(
                            page, url(dc, ac, dep, ret, cls))
                    except Exception as exc:  # noqa: BLE001 - one bad page must not kill the run
                        print(f"  ! {dc}->{ac} {dep} {cabin}: {type(exc).__name__}",
                              file=sys.stderr)
                        price, airlines, status = None, [], "timeout"
                    finally:
                        page.close()

                    if price is None:
                        if status == "no-nonstop":
                            no_service += 1
                            print(f"  {cabin:8} {dc}->{ac} {dep}  no nonstop service")
                        else:
                            misses += 1
                        continue

                    key = f"{cabin}|{dc}{ac}|{dep}|{ret}"
                    prev = old.get(key)
                    new[key] = price
                    why = None
                    if price <= THRESHOLDS[cabin]:
                        why = f"低于阈值 CA${THRESHOLDS[cabin]:,}"
                    elif (prev and prev - price >= DROP_DELTA
                          and price <= DROP_CEILING[cabin]):
                        why = f"比上次跌了 CA${prev - price:,}"
                    if why:
                        alerts.append({
                            "cabin": cabin, "cabin_zh": CABIN_ZH[cabin],
                            "route": f"{CITY_ZH.get(dc,dc)}→{CITY_ZH.get(ac,ac)}",
                            "dep": dep, "ret": ret, "days": days, "price": price,
                            "prev": prev, "why": why, "mu_unpriced": False,
                            "source": "Trip.com",
                            "airlines": airlines,
                            "url": url(dc, ac, dep, ret, cls),
                        })
                    flag = "  <<<" if why else ""
                    was = f"  (was {prev:,})" if prev else ""
                    print(f"  {cabin:8} {dc}->{ac} {dep} -> {ret} ({days}d)  "
                          f"CA${price:,}{was}  {','.join(airlines) or '?'}{flag}")
                    time.sleep(random.uniform(2, 5))
        finally:
            browser.close()

    # Every query failing means Trip.com stopped rendering for us, which looks
    # identical to "nothing is cheap" unless we say so.
    scraped = attempts - no_service
    blocked = scraped > 0 and misses == scraped
    alerts.sort(key=lambda a: a["price"])
    os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
    json.dump({"updated": datetime.now().astimezone().isoformat(timespec="seconds"),
               "prices": new}, open(STATE_PATH, "w"), indent=1, sort_keys=True)
    json.dump({"alerts": alerts, "blocked": blocked, "hard_blocked": blocked,
               "soft_blocked": False, "attempts": attempts, "failures": misses,
               "no_service": no_service,
               "canary": f"{scraped - misses}/{scraped}"},
              open(ALERTS_PATH, "w"), indent=1, ensure_ascii=False)
    print(f"\nattempts={attempts} priced={attempts - misses - no_service} "
          f"no-nonstop={no_service} misses={misses} "
          f"alerts={len(alerts)} blocked={blocked}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
