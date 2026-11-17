#!/usr/bin/env python3
"""Refresh the US rows in data.json from the free MassOutage API, then run build.py.

Source: https://massoutage.com/developers (free, no key, CC BY 4.0; credit
"MassOutage (massoutage.com)" with a link, which every US row's source link does).
One request per run. Keyless limit is 5 requests per 5 minutes per IP, so an
hourly run is far inside it.

What it does (same shape as the old hourly routine):
  * replaces only the US rows (the "United States" total row plus the top 5
    states/territories by customers out, ranked by counts) and the top-level
    as_of in data.json; every worldwide row is left exactly as it is;
  * runs `python3 build.py` so the static pages match.

Sanity checks; if any fails it exits 1 and changes nothing:
  * live status is not "delayed" and the data is under 45 minutes old;
  * national total is above zero;
  * the state sum is within 15% of the national total.

Usage: python3 scripts/fetch_us_outages.py [--dry-run] [--no-build]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / "data.json"
API_URL = "https://massoutage.com/api/v1/live/summary"
SOURCES_URL = "https://massoutage.com/api/v1/live/sources"
SITE_URL = "https://massoutage.com/"
USER_AGENT = "blackoutsmap.com outage map (+https://blackoutsmap.com)"
CT = ZoneInfo("America/Chicago")
TOP_N = 5
MAX_AGE_MIN = 45
MAX_SUM_GAP = 0.15
# Feed-dropout guard: a big state falling sharply while MassOutage reports
# feeds down is usually one utility's feed failing (seen Nov 17: Southern
# California Edison's ~7,000 vanished in one poll). Skip such a run and keep
# the previous numbers, unless those are older than KEEP_OLD_MAX_HOURS.
DROP_MIN_CUSTOMERS = 3000
DROP_MIN_SHARE = 0.6
KEEP_OLD_MAX_HOURS = 3

# State/territory name -> approximate geographic center (same values as fetch_odin.py).
CENTERS = {
    "Alabama": (32.7794, -86.8287), "Alaska": (64.0685, -152.2782),
    "Arizona": (34.2744, -111.6602), "Arkansas": (34.8938, -92.4426),
    "California": (37.1661, -119.4494), "Colorado": (38.9972, -105.5478),
    "Connecticut": (41.6219, -72.7273), "Delaware": (38.9896, -75.5050),
    "District of Columbia": (38.9101, -77.0147), "Florida": (28.6305, -82.4497),
    "Georgia": (32.6469, -83.4426), "Hawaii": (20.2927, -156.3737),
    "Idaho": (44.3509, -114.6130), "Illinois": (40.0417, -89.1965),
    "Indiana": (39.8950, -86.2706), "Iowa": (42.0751, -93.4960),
    "Kansas": (38.4937, -98.3804), "Kentucky": (37.5347, -85.3021),
    "Louisiana": (31.0689, -91.9968), "Maine": (45.3695, -69.2428),
    "Maryland": (39.0550, -76.7909), "Massachusetts": (42.2596, -71.8083),
    "Michigan": (44.3467, -85.4102), "Minnesota": (46.2807, -94.3053),
    "Mississippi": (32.7364, -89.6678), "Missouri": (38.3566, -92.4580),
    "Montana": (47.0527, -109.6333), "Nebraska": (41.5378, -99.7951),
    "Nevada": (39.3289, -116.6312), "New Hampshire": (43.6805, -71.5811),
    "New Jersey": (40.1907, -74.6728), "New Mexico": (34.4071, -106.1126),
    "New York": (42.9538, -75.5268), "North Carolina": (35.5557, -79.3877),
    "North Dakota": (47.4501, -100.4659), "Ohio": (40.2862, -82.7937),
    "Oklahoma": (35.5889, -97.4943), "Oregon": (43.9336, -120.5583),
    "Pennsylvania": (40.9133, -77.8467), "Rhode Island": (41.6762, -71.5562),
    "South Carolina": (33.8361, -81.1637), "South Dakota": (44.4443, -100.2263),
    "Tennessee": (35.8580, -86.3505), "Texas": (31.1761, -99.3453),
    "Utah": (39.3055, -111.6703), "Vermont": (44.0687, -72.6658),
    "Virginia": (37.4316, -78.6569), "Washington": (47.3826, -120.4472),
    "West Virginia": (38.6409, -80.6227), "Wisconsin": (44.6243, -89.9941),
    "Wyoming": (42.9957, -107.5512), "Puerto Rico": (18.2208, -66.5901),
    "Guam": (13.4443, 144.7937), "American Samoa": (-14.2710, -170.1322),
    "Northern Mariana Islands": (15.0979, 145.6739),
}


def fail(msg: str) -> None:
    print(f"ERROR: {msg}; data.json not changed", file=sys.stderr)
    sys.exit(1)


def fetch(url: str = API_URL) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except Exception as exc:  # network error, 429, 5xx
            last = exc
            if attempt < 2:
                time.sleep(10 * (attempt + 1))
    fail(f"could not fetch {url}: {last}")
    return {}


def clock(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0") + " CT"


def is_us_row(row: dict) -> bool:
    return (row.get("region") or "") == "United States" or (row.get("place") or "") == "United States"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print the new US rows, write nothing")
    ap.add_argument("--no-build", action="store_true", help="skip running build.py")
    args = ap.parse_args()

    payload = fetch()
    data = payload.get("data") or {}
    meta = payload.get("meta") or {}
    national = data.get("national") or {}
    states = [s for s in (data.get("states") or []) if s.get("name")]

    status = (meta.get("live") or {}).get("status")
    if status and status != "ok":
        fail(f"MassOutage live status is {status!r}")
    as_of_raw = national.get("as_of") or national.get("updated_at") or meta.get("last_modified")
    try:
        as_of = datetime.fromisoformat(as_of_raw.replace("Z", "+00:00"))
    except Exception:
        fail(f"bad as_of {as_of_raw!r}")
    age_min = (datetime.now(timezone.utc) - as_of).total_seconds() / 60
    if age_min > MAX_AGE_MIN:
        fail(f"data is {age_min:.0f} minutes old")
    total = int(national.get("customers_out") or 0)
    if total <= 0:
        fail("national total is zero")
    state_sum = sum(int(s.get("customers_out") or 0) for s in states)
    if abs(state_sum - total) > MAX_SUM_GAP * total:
        fail(f"state sum {state_sum:,} is too far from national total {total:,}")

    ranked = sorted(states, key=lambda s: int(s.get("customers_out") or 0), reverse=True)
    top = [s for s in ranked if int(s.get("customers_out") or 0) > 0][:TOP_N]

    now_ct = as_of.astimezone(CT)
    updated = clock(now_ct)
    new_rows = [{
        "lat": None, "lon": None, "place": "United States", "region": "Nationwide",
        "cause": "", "affected": f"{total:,} customers out", "start": "", "etr": "",
        "source": SITE_URL, "updated": updated,
    }]
    for s in top:
        name = s["name"]
        lat, lon = CENTERS.get(name, (None, None))
        slug = s.get("slug") or name.lower().replace(" ", "-")
        new_rows.append({
            "lat": lat, "lon": lon, "place": name, "region": "United States",
            "cause": "", "affected": f"{int(s['customers_out']):,} customers out", "start": "", "etr": "",
            "source": f"{SITE_URL}{slug}", "updated": updated,
        })

    doc = json.loads(DATA_PATH.read_text())
    rows = doc.get("outages") or []

    # Feed-dropout guard (see DROP_* above).
    new_by_name = {s["name"]: int(s.get("customers_out") or 0) for s in states}
    drops = []
    for r in rows:
        if (r.get("region") or "") != "United States":
            continue
        try:
            old = int((r.get("affected") or "").split()[0].replace(",", ""))
        except ValueError:
            continue
        new = new_by_name.get(r.get("place"), 0)
        if old - new >= DROP_MIN_CUSTOMERS and new <= old * (1 - DROP_MIN_SHARE):
            drops.append(f"{r.get('place')} {old:,} -> {new:,}")
    if drops:
        feeds = (fetch(SOURCES_URL).get("data") or {}).get("feeds") or {}
        down = int(feeds.get("down") or 0) + int(feeds.get("stale") or 0)
        sys.dont_write_bytecode = True
        sys.path.insert(0, str(ROOT))
        from build import parse_as_of
        prev = parse_as_of(doc.get("as_of") or "")
        old_hours = (datetime.now(CT) - prev).total_seconds() / 3600 if prev else 99
        if down and old_hours < KEEP_OLD_MAX_HOURS:
            print(f"::warning::Skipped: sharp drop ({'; '.join(drops)}) while {down} MassOutage feeds are down/stale; "
                  f"keeping previous numbers ({old_hours:.1f} h old)")
            sys.exit(0)
    insert_at = next((i for i, r in enumerate(rows) if is_us_row(r)), 0)
    kept = [r for r in rows if not is_us_row(r)]
    insert_at = min(insert_at, len(kept))
    doc["outages"] = kept[:insert_at] + new_rows + kept[insert_at:]
    doc["as_of"] = f"{updated}, {now_ct.strftime('%A, %B')} {now_ct.day}, {now_ct.year}"

    print(f"US total {total:,} (state sum {state_sum:,}, data {age_min:.0f} min old)")
    for r in new_rows[1:]:
        print(f"  {r['place']}: {r['affected']}")
    if args.dry_run:
        return
    DATA_PATH.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    if not args.no_build:
        subprocess.run([sys.executable, str(ROOT / "build.py")], cwd=ROOT, check=True)


if __name__ == "__main__":
    main()
