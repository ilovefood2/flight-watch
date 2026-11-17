#!/usr/bin/env python3
"""Scan Google Flights for nonstop YYZ<->PVG fares and flag price drops.

No API key required. Builds Google Flights' own `tfs` protobuf search parameter
and parses the server-rendered HTML, reading prices from the stable
`aria-label="<n> Canadian dollars"` markers.

Scans round trips in BOTH directions (originating in Toronto and originating in
Shanghai) because those are separately-filed fare markets and often priced very
differently.

Each run checks the WATCHLIST plus one rotating slice of the search space, so
the whole window is covered every SLICES runs without hammering Google on any
single run.

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
from datetime import date, datetime, timedelta

# Round trips originating at each end. These are different fare markets.
ROUTES = [tuple(r.split("-")) for r in
          os.environ.get("ROUTES", "YYZ-PVG,PVG-YYZ").split(",")]
CABINS = {"premium": 2, "business": 3}  # Google Flights seat-class enum
CABIN_ZH = {"premium": "超经", "business": "商务"}

STATE_PATH = os.environ.get("STATE_PATH", "state/prices.json")
ALERTS_PATH = os.environ.get("ALERTS_PATH", "alerts.json")

# Hard ceilings (CAD, round trip, incl. tax, 1 adult).
THRESHOLDS = {
    "premium": int(os.environ.get("ALERT_PREMIUM", 3200)),
    "business": int(os.environ.get("ALERT_BUSINESS", 5000)),
}
# Also alert on a drop of at least this much versus the previous reading, but
# only while the fare is low enough to be worth a look. This ceiling is
# deliberately NOT derived from THRESHOLDS: 2027 fares open far above the
# target prices (cheapest business seen in a Jan-Sep sweep was 7,949), so
# gating drops at threshold*1.5 would mute the monitor completely. Threshold
# alerts mean "you hit your price"; drop alerts mean "this is moving".
DROP_DELTA = int(os.environ.get("ALERT_DROP", 150))
DROP_CEILING = {
    "premium": int(os.environ.get("DROP_CEILING_PREMIUM", 5000)),
    "business": int(os.environ.get("DROP_CEILING_BUSINESS", 9000)),
}

WINDOW_START = os.environ.get("WINDOW_START", "2027-01-01")
WINDOW_END = os.environ.get("WINDOW_END", "2027-12-31")
TRIP_LENGTHS = [int(x) for x in os.environ.get("TRIP_LENGTHS", "14,21,28").split(",")]
# Trip length is capped in WEEKDAYS (Mon-Fri, both ends inclusive), not calendar
# days: 20 weekdays is roughly 26-28 calendar days depending on the start day.
MAX_WEEKDAYS = int(os.environ.get("MAX_WEEKDAYS", 20))
SLICES = int(os.environ.get("SLICES", 24))

# Dates worth re-checking on every run: (origin, dest, dep, ret).
WATCHLIST = [
    ("YYZ", "PVG", "2027-02-10", "2027-02-24"),
    ("YYZ", "PVG", "2027-05-12", "2027-05-26"),
    ("PVG", "YYZ", "2027-02-10", "2027-02-24"),
    ("PVG", "YYZ", "2027-05-12", "2027-05-26"),
]

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
PRICE_RE = re.compile(r'aria-label="(\d{3,6}) Canadian dollars"')
MU_UNPRICED = "unavailable. Nonstop flight with China Eastern"


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


def flight_url(dep, ret, seat, origin, dest):
    """Google Flights: round trip, nonstop only, 1 adult, CAD."""
    body = (_vf(1, 28) + _vf(2, 2)
            + _leg(dep, origin, dest) + _leg(ret, dest, origin)
            + _vf(8, 1) + _vf(9, seat) + _vf(14, 1))
    tfs = base64.urlsafe_b64encode(body).decode().rstrip("=")
    return f"https://www.google.com/travel/flights/search?tfs={tfs}&hl=en&curr=CAD"


def cheapest(url, retries=3):
    """Return (lowest price or None, fetch_failed, china_eastern_unpriced)."""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
            with urllib.request.urlopen(req, timeout=45) as resp:
                html = resp.read().decode("utf-8", "ignore")
            hits = [int(x) for x in PRICE_RE.findall(html)]
            # Google server-renders "Total price is unavailable" for China
            # Eastern and fills the number in client-side, so a server-side
            # scrape never sees MU's fare. Flag it rather than silently
            # presenting Air Canada's price as the cheapest nonstop.
            return (min(hits) if hits else None), False, (MU_UNPRICED in html)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt == retries - 1:
                print(f"  ! fetch failed: {exc}", file=sys.stderr)
                return None, True, False
            time.sleep(4 * (attempt + 1))
    return None, True, False


def booking_horizon(cache):
    """Furthest bookable departure. Airlines load ~355 days out, so most of a
    'whole year' window is simply not for sale yet and scanning it is wasted
    requests. Re-probed daily; the horizon advances on its own."""
    today = date.today()
    if cache.get("date") == today.isoformat():
        return date.fromisoformat(cache["horizon"])
    lo, hi = today + timedelta(days=200), today + timedelta(days=400)
    while (hi - lo).days > 3:
        mid = lo + timedelta(days=(hi - lo).days // 2)
        p, _, _ = cheapest(flight_url(mid.isoformat(),
                                      (mid + timedelta(days=14)).isoformat(),
                                      2, *ROUTES[0]))
        if p:
            lo = mid
        else:
            hi = mid
        time.sleep(1.0)
    cache.update(date=today.isoformat(), horizon=lo.isoformat())
    return lo


def weekdays_between(d1, d2):
    """Mon-Fri days from d1 to d2, both ends inclusive."""
    days = (d2 - d1).days + 1
    full_weeks, rem = divmod(days, 7)
    count = full_weeks * 5
    for i in range(rem):
        if (d1 + timedelta(days=full_weeks * 7 + i)).weekday() < 5:
            count += 1
    return count


def build_targets(horizon):
    """(origin, dest, dep, ret) for this run: watchlist + one rotating slice.

    No weekday filter: Air Canada flew YYZ-PVG 4x/week in 2026 but appears to
    operate daily in parts of 2027, and a stale hardcoded schedule fails in the
    worst possible way - silently skipping dates that do have cheap seats.
    """
    start = max(date.fromisoformat(WINDOW_START), date.today() + timedelta(days=1))
    end = min(date.fromisoformat(WINDOW_END), horizon)
    full = []
    d = start
    while d <= end:
        for n in TRIP_LENGTHS:
            ret = d + timedelta(days=n)
            if weekdays_between(d, ret) <= MAX_WEEKDAYS:
                for o, dst in ROUTES:
                    full.append((o, dst, d.isoformat(), ret.isoformat()))
        d += timedelta(days=1)

    slice_no = int(time.time() // 1800) % SLICES
    rotating = [t for i, t in enumerate(full) if i % SLICES == slice_no]
    seen, targets = set(), []
    for t in WATCHLIST + rotating:
        if t not in seen and start.isoformat() <= t[2] <= end.isoformat():
            seen.add(t)
            targets.append(t)
    print(f"horizon {horizon} | slice {slice_no + 1}/{SLICES}: "
          f"{len(targets)} itineraries ({len(full)} in window)")
    return targets


def main():
    state = {}
    if os.path.exists(STATE_PATH):
        try:
            state = json.load(open(STATE_PATH))
        except (json.JSONDecodeError, OSError):
            state = {}
    old = state.get("prices", {})
    horizon_cache = state.get("horizon_cache", {})

    horizon = booking_horizon(horizon_cache)
    new = dict(old)
    alerts = []
    attempts = failures = 0
    canary_total = canary_priced = 0
    watch = set(WATCHLIST)

    for origin, dest, dep, ret in build_targets(horizon):
        days = (date.fromisoformat(ret) - date.fromisoformat(dep)).days
        is_canary = (origin, dest, dep, ret) in watch
        for cabin, seat in CABINS.items():
            key = f"{cabin}|{origin}{dest}|{dep}|{ret}"
            attempts += 1
            canary_total += is_canary
            price, failed, mu_unpriced = cheapest(
                flight_url(dep, ret, seat, origin, dest))
            failures += failed
            if price is None:
                continue
            canary_priced += is_canary

            prev = old.get(key)
            new[key] = price
            why = None
            relevant = price <= DROP_CEILING[cabin]
            if price <= THRESHOLDS[cabin]:
                why = f"低于阈值 CA${THRESHOLDS[cabin]:,}"
            elif prev and prev - price >= DROP_DELTA and relevant:
                why = f"比上次跌了 CA${prev - price:,}（阈值外，但已进入关注区）"
            if why:
                alerts.append({
                    "cabin": cabin, "cabin_zh": CABIN_ZH[cabin],
                    "route": f"{origin}→{dest}", "dep": dep, "ret": ret,
                    "days": days, "price": price, "prev": prev, "why": why,
                    "mu_unpriced": mu_unpriced,
                    "url": flight_url(dep, ret, seat, origin, dest),
                })
            flag = "  <<<" if why else ""
            was = f"  (was {prev:,})" if prev else ""
            print(f"  {cabin:8} {origin}->{dest} {dep} -> {ret} ({days}d)  "
                  f"CA${price:,}{was}{flag}")
            time.sleep(random.uniform(1.0, 2.2))

    # Two ways to be blocked, and the second is the sneaky one:
    #   1. hard failures - the fetch itself threw.
    #   2. soft blocking - HTTP 200 with a page carrying no prices at all,
    #      which is indistinguishable from "no nonstop on this date" unless
    #      you know the date *should* have flights. That's the canary.
    hard_blocked = attempts > 0 and failures / attempts > 0.8
    soft_blocked = canary_total > 0 and canary_priced == 0
    blocked = hard_blocked or soft_blocked

    alerts.sort(key=lambda a: a["price"])
    os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
    json.dump({"updated": datetime.now().astimezone().isoformat(timespec="seconds"),
               "horizon_cache": horizon_cache, "prices": new},
              open(STATE_PATH, "w"), indent=1, sort_keys=True)
    json.dump({"alerts": alerts, "blocked": blocked, "hard_blocked": hard_blocked,
               "soft_blocked": soft_blocked, "attempts": attempts,
               "failures": failures, "canary": f"{canary_priced}/{canary_total}"},
              open(ALERTS_PATH, "w"), indent=1, ensure_ascii=False)

    print(f"\nattempts={attempts} failures={failures} "
          f"canary={canary_priced}/{canary_total} alerts={len(alerts)} "
          f"blocked={blocked}" + (" (soft)" if soft_blocked and not hard_blocked else ""))


if __name__ == "__main__":
    main()
