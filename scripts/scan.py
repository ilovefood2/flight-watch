#!/usr/bin/env python3
"""Scan Google Flights for YYZ<->PVG nonstop fares and flag price drops.

No API key required. Builds Google Flights' own `tfs` protobuf search parameter
and parses the server-rendered HTML, reading prices from the stable
`aria-label="<n> Canadian dollars"` markers.

Two things get checked every run:
  1. WATCHLIST - the specific date pairs we care about most.
  2. One rotating slice of the full Sep-Dec search space, so the whole window
     is covered every SLICES runs without hammering Google on any single run.

Writes state/prices.json (price history) and alerts.json (what to notify on).
"""
import base64
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, timedelta

ORIGIN, DEST = "YYZ", "PVG"
CABINS = {"premium": 2, "business": 3}  # Google Flights seat-class enum
CABIN_ZH = {"premium": "超经", "business": "商务"}

STATE_PATH = os.environ.get("STATE_PATH", "state/prices.json")
ALERTS_PATH = os.environ.get("ALERTS_PATH", "alerts.json")

# Hard ceilings (CAD, round trip, incl. tax, 1 adult). Anything at or below
# these is worth a push; nothing observed in Sep-Dec 2026 has hit them yet.
THRESHOLDS = {
    "premium": int(os.environ.get("ALERT_PREMIUM", 3200)),
    "business": int(os.environ.get("ALERT_BUSINESS", 5000)),
}
# Also alert on any drop of at least this much versus the previous reading -
# but only while the fare is still in shouting distance of the target. A drop
# from 15,828 to 12,188 is a real 3,640 drop and completely useless when the
# ceiling is 5,000, so gate the rule on this multiple of the threshold.
DROP_DELTA = int(os.environ.get("ALERT_DROP", 150))
DROP_RELEVANCE = float(os.environ.get("ALERT_DROP_RELEVANCE", 1.5))

WINDOW_START = os.environ.get("WINDOW_START", "2026-09-01")
WINDOW_END = os.environ.get("WINDOW_END", "2026-12-20")
TRIP_LENGTHS = [
    int(x) for x in os.environ.get("TRIP_LENGTHS", "14,17,21,24,26,28").split(",")
]
# Trip length is capped in WEEKDAYS (Mon-Fri, both ends inclusive), not calendar
# days: 20 weekdays is roughly 26-28 calendar days depending on the start day.
MAX_WEEKDAYS = int(os.environ.get("MAX_WEEKDAYS", 20))
SLICES = int(os.environ.get("SLICES", 6))

# Date pairs worth re-checking on every single run.
WATCHLIST = [
    ("2026-12-02", "2026-12-16"),
    ("2026-11-24", "2026-12-11"),
    ("2026-11-11", "2026-11-25"),
    ("2026-11-17", "2026-12-02"),
    ("2026-12-08", "2026-12-23"),
    ("2026-09-30", "2026-10-12"),
]

# AC27 does not fly daily, so skip weekdays with no departure.
# Mon=0 .. Sun=6.  Summer (through ~Oct 24): Mon/Wed/Fri/Sun.
# Winter (from ~Oct 25): Tue/Wed/Thu/Sat.
SUMMER_DEP = {0, 2, 4, 6}
WINTER_DEP = {1, 2, 3, 5}
SCHED_SWITCH = date(2026, 10, 25)

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
PRICE_RE = re.compile(r'aria-label="(\d{3,6}) Canadian dollars"')


def _vi(n):
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def _ld(f, p):
    return bytes([f << 3 | 2]) + _vi(len(p)) + p


def _vf(f, v):
    return bytes([f << 3 | 0]) + _vi(v)


def _ap(f, code):
    return _ld(f, _vf(1, 1) + _ld(2, code.encode()))


def _leg(d, a, b):
    return _ld(3, _ld(2, d.encode()) + _vf(5, 0) + _ap(13, a) + _ap(14, b))


def flight_url(dep, ret, seat):
    """Google Flights: round trip, nonstop only, 1 adult, CAD."""
    body = (
        _vf(1, 28)
        + _vf(2, 2)
        + _leg(dep, ORIGIN, DEST)
        + _leg(ret, DEST, ORIGIN)
        + _vf(8, 1)
        + _vf(9, seat)
        + _vf(14, 1)
    )
    tfs = base64.urlsafe_b64encode(body).decode().rstrip("=")
    return f"https://www.google.com/travel/flights/search?tfs={tfs}&hl=en&curr=CAD"


def cheapest(url, retries=3):
    """Return (lowest price or None, fetch_failed, china_eastern_unpriced)."""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
            )
            with urllib.request.urlopen(req, timeout=45) as resp:
                html = resp.read().decode("utf-8", "ignore")
            hits = [int(x) for x in PRICE_RE.findall(html)]
            # Google server-renders "Total price is unavailable" for China
            # Eastern and only fills the real number in client-side, so a
            # server-side scrape can never see MU's fare. Flag it instead of
            # silently reporting the Air Canada price as "the" cheapest.
            mu_unpriced = "unavailable. Nonstop flight with China Eastern" in html
            return (min(hits) if hits else None), False, mu_unpriced
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt == retries - 1:
                print(f"  ! fetch failed: {exc}", file=sys.stderr)
                return None, True, False
            time.sleep(4 * (attempt + 1))
    return None, True, False


def dep_dates():
    d = date.fromisoformat(WINDOW_START)
    end = date.fromisoformat(WINDOW_END)
    while d <= end:
        allowed = SUMMER_DEP if d < SCHED_SWITCH else WINTER_DEP
        if d.weekday() in allowed:
            yield d
        d += timedelta(days=1)


def weekdays_between(d1, d2):
    """Mon-Fri days from d1 to d2, both ends inclusive."""
    days = (d2 - d1).days + 1
    full_weeks, rem = divmod(days, 7)
    count = full_weeks * 5
    for i in range(rem):
        if (d1 + timedelta(days=full_weeks * 7 + i)).weekday() < 5:
            count += 1
    return count


def build_targets():
    """(dep, ret) pairs for this run: watchlist + one rotating slice."""
    full = []
    for dep in dep_dates():
        for n in TRIP_LENGTHS:
            ret = dep + timedelta(days=n)
            if weekdays_between(dep, ret) <= MAX_WEEKDAYS:
                full.append((dep.isoformat(), ret.isoformat()))

    slice_no = int(time.time() // 1800) % SLICES
    rotating = [p for i, p in enumerate(full) if i % SLICES == slice_no]

    seen, targets = set(), []
    for pair in WATCHLIST + rotating:
        if pair not in seen:
            seen.add(pair)
            targets.append(pair)
    print(
        f"slice {slice_no + 1}/{SLICES}: {len(targets)} date pairs "
        f"({len(full)} total in window)"
    )
    return targets


def main():
    old = {}
    if os.path.exists(STATE_PATH):
        try:
            old = json.load(open(STATE_PATH)).get("prices", {})
        except (json.JSONDecodeError, OSError):
            old = {}

    new = dict(old)  # carry forward prices for pairs not scanned this run
    alerts = []
    attempts = failures = 0
    # The watchlist pairs are known to have nonstop service, so they double as a
    # canary: if none of them price, we are being served empty pages.
    canary_total = canary_priced = 0
    watch = set(WATCHLIST)

    for dep, ret in build_targets():
        days = (date.fromisoformat(ret) - date.fromisoformat(dep)).days
        is_canary = (dep, ret) in watch
        for cabin, seat in CABINS.items():
            key = f"{cabin}|{dep}|{ret}"
            attempts += 1
            canary_total += is_canary
            price, failed, mu_unpriced = cheapest(flight_url(dep, ret, seat))
            failures += failed
            if price is None:
                continue
            canary_priced += is_canary

            prev = old.get(key)
            new[key] = price

            why = None
            relevant = price <= THRESHOLDS[cabin] * DROP_RELEVANCE
            if price <= THRESHOLDS[cabin]:
                why = f"低于阈值 CA${THRESHOLDS[cabin]:,}"
            elif prev and prev - price >= DROP_DELTA and relevant:
                why = f"比上次跌了 CA${prev - price:,}"
            if why:
                alerts.append(
                    {
                        "cabin": cabin,
                        "cabin_zh": CABIN_ZH[cabin],
                        "dep": dep,
                        "ret": ret,
                        "days": days,
                        "price": price,
                        "prev": prev,
                        "why": why,
                        "mu_unpriced": mu_unpriced,
                        "url": flight_url(dep, ret, seat),
                    }
                )

            flag = "  <<<" if why else ""
            was = f"  (was {prev:,})" if prev else ""
            print(f"  {cabin:8} {dep} -> {ret} ({days}d)  CA${price:,}{was}{flag}")
            time.sleep(random.uniform(1.0, 2.2))  # be polite, vary the cadence

    # Two ways to be blocked, and the second one is the sneaky one:
    #   1. hard failures  - the fetch itself threw.
    #   2. soft blocking  - HTTP 200 with a page that carries no prices at all.
    # Case 2 is indistinguishable from "this date pair has no nonstop combo"
    # unless you know the pair *should* have flights, which is what the
    # watchlist canary is for. Without this check a blocked run looks
    # identical to a run that simply found nothing cheap.
    hard_blocked = attempts > 0 and failures / attempts > 0.8
    soft_blocked = canary_total > 0 and canary_priced == 0
    blocked = hard_blocked or soft_blocked

    alerts.sort(key=lambda a: a["price"])
    os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
    json.dump(
        {"updated": time.strftime("%Y-%m-%d %H:%M:%S %z"), "prices": new},
        open(STATE_PATH, "w"),
        indent=1,
        sort_keys=True,
    )
    json.dump(
        {
            "alerts": alerts,
            "blocked": blocked,
            "hard_blocked": hard_blocked,
            "soft_blocked": soft_blocked,
            "attempts": attempts,
            "failures": failures,
            "canary": f"{canary_priced}/{canary_total}",
        },
        open(ALERTS_PATH, "w"),
        indent=1,
        ensure_ascii=False,
    )

    print(
        f"\nattempts={attempts} failures={failures} "
        f"canary={canary_priced}/{canary_total} "
        f"alerts={len(alerts)} blocked={blocked}"
        + (" (soft)" if soft_blocked and not hard_blocked else "")
    )
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
            fh.write(f"count={len(alerts)}\n")
            fh.write(f"blocked={'true' if blocked else 'false'}\n")


if __name__ == "__main__":
    main()
