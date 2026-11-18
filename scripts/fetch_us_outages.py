#!/usr/bin/env python3
"""Refresh the US rows in data.json from the free MassOutage API, then run build.py.

Source: https://massoutage.com/developers (free, no key, CC BY 4.0; credit
"MassOutage (massoutage.com)" with a link, which every US row's source link does).
One request per run. Keyless limit is 5 requests per 5 minutes per IP, so an
hourly run is far inside it.

A committed us_supplement.json (written by a twice-daily PowerOutage.us check)
is merged in so places MassOutage does not cover (Puerto Rico, the U.S. Virgin
Islands, and other territories) and states where PowerOutage.us is higher still
appear. The hourly job never writes that file. Supplement rows older than 13
hours are ignored; if the whole file is older than 13 hours it is ignored.

What it does (same shape as the old hourly routine):
  * replaces only the US rows (the "United States" total row plus the top 5
    states/territories by customers out, ranked by the merged counts) and the
    top-level as_of in data.json; every worldwide row is left exactly as it is;
  * runs `python3 build.py` so the static pages match.

Sanity checks; if any fails it exits 1 and changes nothing:
  * live status is not "delayed" and the MassOutage data is under 45 minutes old;
  * national total is above zero (after the merge as well);
  * the state sum is within 15% of the national total (MassOutage and merged).

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
from urllib.parse import quote
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / "data.json"
SUPPLEMENT_PATH = ROOT / "us_supplement.json"
API_URL = "https://massoutage.com/api/v1/live/summary"
SOURCES_URL = "https://massoutage.com/api/v1/live/sources"
SITE_URL = "https://massoutage.com/"
PO_SITE = "https://poweroutage.us/"
USER_AGENT = "blackoutsmap.com outage map (+https://blackoutsmap.com)"
CT = ZoneInfo("America/Chicago")
TOP_N = 5
MAX_AGE_MIN = 45
MAX_SUM_GAP = 0.15
SUPPLEMENT_MAX_HOURS = 13
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
    "U.S. Virgin Islands": (18.338, -64.894),
    "Guam": (13.4443, 144.7937), "American Samoa": (-14.2710, -170.1322),
    "Northern Mariana Islands": (15.0979, 145.6739),
}

# Canonical names for spellings that show up on PowerOutage.us or in supplement files.
NAME_ALIASES = {
    "us virgin islands": "U.S. Virgin Islands",
    "u.s. virgin islands": "U.S. Virgin Islands",
    "united states virgin islands": "U.S. Virgin Islands",
    "virgin islands": "U.S. Virgin Islands",
    "usvi": "U.S. Virgin Islands",
    "puerto rico": "Puerto Rico",
    "district of columbia": "District of Columbia",
    "washington dc": "District of Columbia",
    "washington, dc": "District of Columbia",
    "washington, d.c.": "District of Columbia",
    "washington d.c.": "District of Columbia",
    "d.c.": "District of Columbia",
    "dc": "District of Columbia",
}

# PowerOutage.us state-page paths that are not just the lowercased name.
PO_PATHS = {
    "U.S. Virgin Islands": "https://poweroutage.us/area/state/virgin%20islands",
    "Puerto Rico": "https://poweroutage.us/area/state/puerto%20rico",
}

SKIP_SUPPLEMENT_NAMES = {"united states", "nationwide", "us", "u.s.", "usa"}


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


def canonical_name(name: str) -> str:
    raw = " ".join((name or "").split())
    if not raw:
        return ""
    alias = NAME_ALIASES.get(raw.lower())
    if alias:
        return alias
    # Preserve the MassOutage / CENTERS spelling when only case differs.
    for known in CENTERS:
        if known.lower() == raw.lower():
            return known
    return raw


def parse_stamp(raw, fallback: datetime | None = None) -> datetime | None:
    """Parse an ISO-8601 or '6:00 PM CT, Tuesday, November 17, 2026' timestamp."""
    if raw is None or raw == "":
        return fallback
    if isinstance(raw, (int, float)):
        # Unix seconds or milliseconds.
        ts = float(raw)
        if ts > 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(CT)
    text = str(raw).strip()
    if not text:
        return fallback
    iso = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CT)
        return dt.astimezone(CT)
    except ValueError:
        pass
    # Full "h:mm AM CT, Weekday, Month D, YYYY"
    import re
    match = re.search(
        r"(\d{1,2}:\d{2}\s*[AP]M)\s*CT,\s*[A-Za-z]+,\s*([A-Za-z]+ \d{1,2}, \d{4})",
        text,
    )
    if match:
        try:
            naive = datetime.strptime(f"{match.group(2)} {match.group(1)}", "%B %d, %Y %I:%M %p")
            return naive.replace(tzinfo=CT)
        except ValueError:
            return fallback
    # Time only, e.g. "6:00 PM CT" — date comes from fallback if present.
    match = re.search(r"(\d{1,2}:\d{2}\s*[AP]M)\s*CT", text)
    if match and fallback is not None:
        try:
            t = datetime.strptime(match.group(1), "%I:%M %p").time()
            return fallback.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        except ValueError:
            return fallback
    return fallback


def po_source_url(name: str) -> str:
    if name in PO_PATHS:
        return PO_PATHS[name]
    return f"{PO_SITE}area/state/{quote(name.lower())}"


def load_supplement(path: Path, now: datetime) -> dict[str, dict]:
    """Return canonical-name -> {customers_out, as_of, source_url} for fresh rows.

    Missing or unreadable files yield an empty dict (the hourly job still runs).
    A file-level as_of older than SUPPLEMENT_MAX_HOURS drops the whole file.
    """
    if not path.is_file():
        return {}
    try:
        doc = json.loads(path.read_text())
    except Exception as exc:
        print(f"::warning::Ignoring {path.name}: could not read ({exc})")
        return {}
    if not isinstance(doc, dict):
        print(f"::warning::Ignoring {path.name}: expected a JSON object")
        return {}
    file_as_of = parse_stamp(doc.get("as_of"))
    if file_as_of is not None:
        age_h = (now - file_as_of).total_seconds() / 3600
        if age_h > SUPPLEMENT_MAX_HOURS:
            print(f"Ignoring {path.name}: as_of is {age_h:.1f} h old (limit {SUPPLEMENT_MAX_HOURS} h)")
            return {}
    places = doc.get("places")
    if not isinstance(places, list):
        print(f"::warning::Ignoring {path.name}: missing places list")
        return {}
    out: dict[str, dict] = {}
    skipped_stale = 0
    for raw in places:
        if not isinstance(raw, dict):
            continue
        name = canonical_name(str(raw.get("name") or ""))
        if not name or name.lower() in SKIP_SUPPLEMENT_NAMES:
            continue
        try:
            count = int(raw.get("customers_out") or 0)
        except (TypeError, ValueError):
            continue
        if count < 0:
            continue
        row_as_of = parse_stamp(raw.get("as_of"), file_as_of)
        if row_as_of is None:
            print(f"::warning::Ignoring supplement row {name!r}: no as_of")
            continue
        age_h = (now - row_as_of).total_seconds() / 3600
        if age_h > SUPPLEMENT_MAX_HOURS:
            skipped_stale += 1
            continue
        out[name] = {
            "customers_out": count,
            "as_of": row_as_of,
            "source_url": po_source_url(name),
        }
    if skipped_stale:
        print(f"Ignored {skipped_stale} stale supplement row(s) older than {SUPPLEMENT_MAX_HOURS} h")
    if out:
        print(f"Loaded {len(out)} supplement place(s) from {path.name}")
    return out


def merge_with_supplement(states: list[dict], total: int, supplement: dict[str, dict]) -> tuple[list[dict], int, set[str]]:
    """Merge MassOutage state rows with a fresh PowerOutage.us supplement.

    Uncovered places (not in the MassOutage list) are added from the supplement.
    Covered states use max(MassOutage, supplement). The national total is
    raised by the same additions. Returns (merged_states, merged_total, used).
    """
    if not supplement:
        return states, total, set()
    by_name: dict[str, dict] = {}
    for s in states:
        name = canonical_name(s.get("name") or "")
        if not name:
            continue
        by_name[name] = {
            "name": name,
            "customers_out": int(s.get("customers_out") or 0),
            "slug": s.get("slug") or name.lower().replace(" ", "-"),
            "from_supplement": False,
            "supplement_as_of": None,
        }
    used: set[str] = set()
    added = 0
    for name, info in supplement.items():
        supp_n = int(info["customers_out"])
        if name in by_name:
            current = by_name[name]["customers_out"]
            if supp_n > current:
                added += supp_n - current
                by_name[name]["customers_out"] = supp_n
                by_name[name]["from_supplement"] = True
                by_name[name]["supplement_as_of"] = info["as_of"]
                used.add(name)
        else:
            # Place MassOutage does not cover (PR, USVI, and other territories).
            if supp_n <= 0:
                continue
            added += supp_n
            by_name[name] = {
                "name": name,
                "customers_out": supp_n,
                "slug": name.lower().replace(" ", "-").replace(".", ""),
                "from_supplement": True,
                "supplement_as_of": info["as_of"],
            }
            used.add(name)
    return list(by_name.values()), total + added, used


def check_totals(total: int, states: list[dict], label: str) -> None:
    if total <= 0:
        fail(f"{label} national total is zero")
    state_sum = sum(int(s.get("customers_out") or 0) for s in states)
    if abs(state_sum - total) > MAX_SUM_GAP * total:
        fail(f"{label} state sum {state_sum:,} is too far from national total {total:,}")


def build_rows(total: int, top: list[dict], mo_updated: str, supplement: dict[str, dict], used: set[str]) -> list[dict]:
    new_rows = [{
        "lat": None, "lon": None, "place": "United States", "region": "Nationwide",
        "cause": "", "affected": f"{total:,} customers out", "start": "", "etr": "",
        "source": SITE_URL, "updated": mo_updated,
    }]
    if used:
        new_rows[0]["source_label"] = "MassOutage + PowerOutage.us"
    for s in top:
        name = s["name"]
        lat, lon = CENTERS.get(name, (None, None))
        if s.get("from_supplement"):
            updated = clock(s["supplement_as_of"]) if s.get("supplement_as_of") else mo_updated
            src = (supplement.get(name) or {}).get("source_url") or po_source_url(name)
            new_rows.append({
                "lat": lat, "lon": lon, "place": name, "region": "United States",
                "cause": "", "affected": f"{int(s['customers_out']):,} customers out", "start": "", "etr": "",
                "source": src, "updated": updated,
            })
        else:
            slug = s.get("slug") or name.lower().replace(" ", "-")
            new_rows.append({
                "lat": lat, "lon": lon, "place": name, "region": "United States",
                "cause": "", "affected": f"{int(s['customers_out']):,} customers out", "start": "", "etr": "",
                "source": f"{SITE_URL}{slug}", "updated": mo_updated,
            })
    return new_rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print the new US rows, write nothing")
    ap.add_argument("--no-build", action="store_true", help="skip running build.py")
    ap.add_argument("--supplement", default=str(SUPPLEMENT_PATH), help="path to us_supplement.json")
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
    mo_total = int(national.get("customers_out") or 0)
    check_totals(mo_total, states, "MassOutage")

    now_ct = datetime.now(CT)
    supplement = load_supplement(Path(args.supplement), now_ct)
    merged_states, total, used = merge_with_supplement(states, mo_total, supplement)
    check_totals(total, merged_states, "merged")

    ranked = sorted(merged_states, key=lambda s: int(s.get("customers_out") or 0), reverse=True)
    top = [s for s in ranked if int(s.get("customers_out") or 0) > 0][:TOP_N]

    mo_ct = as_of.astimezone(CT)
    updated = clock(mo_ct)
    new_rows = build_rows(total, top, updated, supplement, used)

    doc = json.loads(DATA_PATH.read_text())
    rows = doc.get("outages") or []

    # Feed-dropout guard (see DROP_* above). Compare previous rows to the
    # merged counts so a filled-in territory or a max() swap is not a drop.
    new_by_name = {s["name"]: int(s.get("customers_out") or 0) for s in merged_states}
    drops = []
    for r in rows:
        if (r.get("region") or "") != "United States":
            continue
        try:
            old = int((r.get("affected") or "").split()[0].replace(",", ""))
        except ValueError:
            continue
        if r.get("place") not in new_by_name:
            continue  # not in this snapshot (e.g. expired territory supplement)
        new = new_by_name[r["place"]]
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
    doc["as_of"] = f"{updated}, {mo_ct.strftime('%A, %B')} {mo_ct.day}, {mo_ct.year}"

    mo_sum = sum(int(s.get("customers_out") or 0) for s in states)
    merged_sum = sum(int(s.get("customers_out") or 0) for s in merged_states)
    print(f"MassOutage {mo_total:,} (state sum {mo_sum:,}, data {age_min:.0f} min old)")
    print(f"Merged US total {total:,} (state sum {merged_sum:,}; supplement used for {', '.join(sorted(used)) or 'nothing'})")
    for r in new_rows[1:]:
        print(f"  {r['place']}: {r['affected']}  [{r.get('source_label') or r['source']}]  {r['updated']}")
    if args.dry_run:
        return
    DATA_PATH.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    if not args.no_build:
        subprocess.run([sys.executable, str(ROOT / "build.py")], cwd=ROOT, check=True)


if __name__ == "__main__":
    main()
