#!/usr/bin/env python3
"""Fetch current US power outages from DOE/ORNL ODIN and summarize them.

ODIN (Outage Data Initiative Nationwide) is run by Oak Ridge National Laboratory
for the U.S. Department of Energy. This script uses ODIN's public, no-key
county-level endpoint (documented at https://odin.ornl.gov/v3/api-docs):

    GET https://odin.ornl.gov/odi?format=JSON      current outages by county
    GET https://odin.ornl.gov/odi/status           utilities reporting + last update

If that fails, it falls back to the same data mirrored on ORNL's Open Energy
Data Portal (dataset odin-real-time-outages-county, refreshed about every 15 min).

It produces the same US rows the site's data.json uses (a "United States /
Nationwide" row plus the top 5 states by customers out), in data.json row shape.

SHADOW MODE: by default this only writes the summary JSON and appends to a CSV
log. It never touches data.json. Stdlib only.

Usage:
    python3 scripts/fetch_odin.py --out shadow/odin_us.json --log shadow/odin_log.csv \
        [--compare data.json]
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ODI_URL = "https://odin.ornl.gov/odi?format=JSON"
STATUS_URL = "https://odin.ornl.gov/odi/status"
ODS_URL = (
    "https://ornl.opendatasoft.com/api/explore/v2.1/catalog/datasets/"
    "odin-real-time-outages-county/exports/json?"
    + urllib.parse.urlencode({"select": "communitydescriptor,metersaffected,name,reportedstarttime"})
)
PUBLIC_SOURCE = "https://odin.ornl.gov/"
USER_AGENT = "blackoutsmap.com outage map (+https://blackoutsmap.com)"
CT = ZoneInfo("America/Chicago")
TOP_N = 5
STALE_START_DAYS = 14      # outage records that started longer ago than this are flagged
STALE_FEED_HOURS = 6       # utilities whose last ODIN update is older than this are flagged

# 2-digit state FIPS -> (name, lat, lon). Approximate geographic centers.
STATES = {
    "01": ("Alabama", 32.7794, -86.8287), "02": ("Alaska", 64.0685, -152.2782),
    "04": ("Arizona", 34.2744, -111.6602), "05": ("Arkansas", 34.8938, -92.4426),
    "06": ("California", 37.1661, -119.4494), "08": ("Colorado", 38.9972, -105.5478),
    "09": ("Connecticut", 41.6219, -72.7273), "10": ("Delaware", 38.9896, -75.5050),
    "11": ("District of Columbia", 38.9101, -77.0147), "12": ("Florida", 28.6305, -82.4497),
    "13": ("Georgia", 32.6469, -83.4426), "15": ("Hawaii", 20.2927, -156.3737),
    "16": ("Idaho", 44.3509, -114.6130), "17": ("Illinois", 40.0417, -89.1965),
    "18": ("Indiana", 39.8950, -86.2706), "19": ("Iowa", 42.0751, -93.4960),
    "20": ("Kansas", 38.4937, -98.3804), "21": ("Kentucky", 37.5347, -85.3021),
    "22": ("Louisiana", 31.0689, -91.9968), "23": ("Maine", 45.3695, -69.2428),
    "24": ("Maryland", 39.0550, -76.7909), "25": ("Massachusetts", 42.2596, -71.8083),
    "26": ("Michigan", 44.3467, -85.4102), "27": ("Minnesota", 46.2807, -94.3053),
    "28": ("Mississippi", 32.7364, -89.6678), "29": ("Missouri", 38.3566, -92.4580),
    "30": ("Montana", 47.0527, -109.6333), "31": ("Nebraska", 41.5378, -99.7951),
    "32": ("Nevada", 39.3289, -116.6312), "33": ("New Hampshire", 43.6805, -71.5811),
    "34": ("New Jersey", 40.1907, -74.6728), "35": ("New Mexico", 34.4071, -106.1126),
    "36": ("New York", 42.9538, -75.5268), "37": ("North Carolina", 35.5557, -79.3877),
    "38": ("North Dakota", 47.4501, -100.4659), "39": ("Ohio", 40.2862, -82.7937),
    "40": ("Oklahoma", 35.5889, -97.4943), "41": ("Oregon", 43.9336, -120.5583),
    "42": ("Pennsylvania", 40.9133, -77.8467), "44": ("Rhode Island", 41.6762, -71.5562),
    "45": ("South Carolina", 33.8361, -81.1637), "46": ("South Dakota", 44.4443, -100.2263),
    "47": ("Tennessee", 35.8580, -86.3505), "48": ("Texas", 31.1761, -99.3453),
    "49": ("Utah", 39.3055, -111.6703), "50": ("Vermont", 44.0687, -72.6658),
    "51": ("Virginia", 37.4316, -78.6569), "53": ("Washington", 47.3826, -120.4472),
    "54": ("West Virginia", 38.6409, -80.6227), "55": ("Wisconsin", 44.6243, -89.9941),
    "56": ("Wyoming", 42.9957, -107.5512), "60": ("American Samoa", -14.2710, -170.1322),
    "66": ("Guam", 13.4443, 144.7937), "69": ("Northern Mariana Islands", 15.0979, 145.6739),
    "72": ("Puerto Rico", 18.2208, -66.5901), "78": ("U.S. Virgin Islands", 18.3380, -64.8940),
}


def get_json(url: str, attempts: int = 3, timeout: int = 60):
    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except Exception as exc:  # noqa: BLE001 - retry any network/parse error
            last = exc
            time.sleep(5 * (i + 1))
    raise RuntimeError(f"GET {url} failed after {attempts} attempts: {last}")


def parse_time(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def load_records() -> tuple[list[dict], str]:
    """Return normalized records [{fips, customers, utility, start}] and the source used."""
    try:
        data = get_json(ODI_URL)
        out = []
        for o in data.get("outage", []):
            names = {n.get("nameType"): n.get("name") for n in (o.get("names") or [])}
            out.append({
                "fips": str(o.get("communityDescriptor") or ""),
                "customers": int(o.get("metersAffected") or 0),
                "utility": names.get("UtilityName") or "Unknown utility",
                "utility_id": names.get("UtilityId") or "",
                "start": o.get("reportedStartTime"),
            })
        if not out:
            raise RuntimeError("ODIN /odi returned zero outage records")
        return out, ODI_URL
    except Exception as exc:  # noqa: BLE001
        print(f"warning: primary ODIN endpoint failed ({exc}); using ORNL portal mirror", file=sys.stderr)
    data = get_json(ODS_URL, timeout=120)
    out = []
    for o in data:
        name = o.get("name") or "Unknown utility"
        util, _, uid = name.rpartition(",") if "," in name else (name, "", "")
        out.append({
            "fips": str(o.get("communitydescriptor") or ""),
            "customers": int(o.get("metersaffected") or 0),
            "utility": util or name,
            "utility_id": uid,
            "start": o.get("reportedstarttime"),
        })
    return out, ODS_URL


def load_status(now: datetime) -> dict:
    try:
        data = get_json(STATUS_URL)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    fresh = stale = errors = 0
    for u in data:
        received = parse_time(u.get("receivedDate"))
        if u.get("hasError"):
            errors += 1
        if received and now - received <= timedelta(hours=STALE_FEED_HOURS):
            fresh += 1
        else:
            stale += 1
    return {"utilities_listed": len(data), "updated_within_6h": fresh, "stale_6h_plus": stale, "with_error": errors}


def fmt_ct(dt: datetime) -> str:
    return dt.astimezone(CT).strftime("%-I:%M %p CT")


def summarize(records: list[dict], now: datetime) -> dict:
    by_state = defaultdict(lambda: {"customers_out": 0, "records": 0, "utilities": set()})
    unknown_fips = 0
    stale_customers = 0
    for r in records:
        code = r["fips"][:2]
        if code not in STATES:
            unknown_fips += r["customers"]
            continue
        s = by_state[code]
        s["customers_out"] += r["customers"]
        s["records"] += 1
        s["utilities"].add(r["utility"])
        start = parse_time(r["start"])
        if start and now - start > timedelta(days=STALE_START_DAYS):
            stale_customers += r["customers"]
    states = sorted(
        (
            {"fips": code, "state": STATES[code][0], "customers_out": v["customers_out"],
             "records": v["records"], "utilities": sorted(v["utilities"])}
            for code, v in by_state.items() if v["customers_out"] > 0
        ),
        key=lambda s: (-s["customers_out"], s["state"]),
    )
    total = sum(s["customers_out"] for s in states)
    return {
        "total": total,
        "states": states,
        "states_with_outages": len(states),
        "records": len(records),
        "unmapped_customers": unknown_fips,
        "customers_in_records_older_than_14d": stale_customers,
    }


def data_json_rows(summary: dict, now: datetime) -> list[dict]:
    """Rows shaped exactly like the US rows in data.json."""
    updated = fmt_ct(now)
    rows = [{
        "lat": None, "lon": None, "place": "United States", "region": "Nationwide",
        "cause": "", "affected": f"{summary['total']:,} customers out", "start": "", "etr": "",
        "source": PUBLIC_SOURCE, "updated": updated,
    }]
    for s in summary["states"][:TOP_N]:
        _, lat, lon = STATES[s["fips"]]
        rows.append({
            "lat": lat, "lon": lon, "place": s["state"], "region": "United States",
            "cause": "", "affected": f"{s['customers_out']:,} customers out", "start": "", "etr": "",
            "source": PUBLIC_SOURCE, "updated": updated,
        })
    return rows


def parse_count(text: str) -> int | None:
    digits = "".join(ch for ch in (text or "").split(" customers")[0] if ch.isdigit())
    return int(digits) if digits else None


def poweroutage_us_snapshot(path: Path) -> dict | None:
    """Read the current poweroutage.us-sourced US rows from data.json for comparison."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    rows = [r for r in data.get("outages", []) if "poweroutage.us" in (r.get("source") or "")]
    total = next((parse_count(r.get("affected")) for r in rows if r.get("place") == "United States"), None)
    states = [(r.get("place"), parse_count(r.get("affected"))) for r in rows
              if r.get("region") == "United States" and r.get("place") != "United States"]
    return {"as_of": data.get("as_of"), "total": total, "top5": states[:TOP_N]}


def top5_str(pairs) -> str:
    return "; ".join(f"{name} {count:,}" for name, count in pairs if count is not None)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="shadow/odin_us.json")
    ap.add_argument("--log", default="shadow/odin_log.csv")
    ap.add_argument("--compare", default="data.json", help="data.json to compare against (read only)")
    args = ap.parse_args()

    now = datetime.now(timezone.utc).replace(microsecond=0)
    records, source = load_records()
    summary = summarize(records, now)
    status = load_status(now)
    pous = poweroutage_us_snapshot(Path(args.compare)) if args.compare else None

    result = {
        "generated_at_utc": now.isoformat().replace("+00:00", "Z"),
        "generated_at_ct": now.astimezone(CT).strftime("%-I:%M %p CT, %A, %B %-d, %Y"),
        "mode": "shadow (not used by the live site)",
        "source": source,
        "fallback_used": source != ODI_URL,
        "attribution": "Outage Data Initiative Nationwide (ODIN), Oak Ridge National Laboratory / U.S. DOE",
        "reporting_status": status,
        **summary,
        "rows": data_json_rows(summary, now),
        "poweroutage_us_in_data_json": pous,
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")

    log = Path(args.log)
    new = not log.exists()
    with log.open("a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["timestamp_utc", "odin_total", "odin_top5", "odin_records", "odin_source",
                        "pous_as_of", "pous_total", "pous_top5"])
        w.writerow([
            result["generated_at_utc"], summary["total"],
            top5_str((s["state"], s["customers_out"]) for s in summary["states"][:TOP_N]),
            summary["records"], "odi" if source == ODI_URL else "portal",
            (pous or {}).get("as_of", ""), (pous or {}).get("total", ""),
            top5_str((pous or {}).get("top5", [])),
        ])

    print(f"ODIN total {summary['total']:,} customers out across {summary['states_with_outages']} states "
          f"({summary['records']} records, source {source}).")
    for s in summary["states"][:TOP_N]:
        print(f"  {s['state']}: {s['customers_out']:,}")
    if pous:
        print(f"poweroutage.us (data.json, {pous['as_of']}): total {pous['total']:,}; {top5_str(pous['top5'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
