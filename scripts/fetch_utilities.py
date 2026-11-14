#!/usr/bin/env python3
"""Free US outage aggregator: utility outage maps + DOE/ORNL ODIN (SHADOW MODE).

For each large US utility we read the same public JSON its own outage map loads
(Kubra Storm Center, ESRI/ArcGIS, DataCapable, or the utility's own JSON) and
turn it into "customers out by state". Utilities without an adapter here come
from ODIN (scripts/fetch_odin.py). A utility present in both is counted once,
preferring the direct map; if a direct adapter fails, that utility's ODIN
records are used instead.

Politeness: one request set per run (run every 30 min by GitHub Actions), a
clear User-Agent, timeouts, at most one retry, a small worker pool. Utilities
whose robots.txt disallows the data path are not fetched (see SKIPPED).

SHADOW MODE: writes shadow/combined_us.json and appends shadow/combined_log.csv.
It never touches data.json or the live site. Stdlib only.

Usage:
    python3 scripts/fetch_utilities.py --out shadow/combined_us.json \
        --log shadow/combined_log.csv --compare data.json
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_odin as odin  # noqa: E402  (same folder; reuses ODIN loader + state table)

USER_AGENT = odin.USER_AGENT
TIMEOUT = 25
TOP_N = 5
US_CUSTOMERS = 162_000_000  # approx. US electricity customer accounts (EIA)

NAME_TO_FIPS = {v[0].upper(): k for k, v in odin.STATES.items()}
ABBR = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
    "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming", "PR": "Puerto Rico",
}


def state_name(text: str) -> str | None:
    """Normalize 'TX' / 'Texas' / 'TEXAS' to 'Texas'; None if not a state."""
    t = (text or "").strip()
    if t.upper() in ABBR:
        return ABBR[t.upper()]
    if t.upper() in NAME_TO_FIPS:
        return odin.STATES[NAME_TO_FIPS[t.upper()]][0]
    return None


def get(url: str, attempts: int = 2, raw: bool = False):
    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": USER_AGENT, "Accept": "application/json", "Accept-Encoding": "gzip"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                body = resp.read()
            if body[:2] == b"\x1f\x8b":
                body = gzip.decompress(body)
            return body if raw else json.loads(body.decode("utf-8-sig"))
        except Exception as exc:  # noqa: BLE001
            last = exc
            if i + 1 < attempts:
                time.sleep(3)
    raise RuntimeError(f"{url}: {last}")


def num(v) -> int:
    if isinstance(v, dict):
        v = v.get("val")
    if isinstance(v, str):
        v = v.replace(",", "").strip() or 0
    return int(float(v or 0))


# ---------------------------------------------------------------- adapters
# Each adapter returns {"states": {state_name: customers_out}, "served": int|None}.

KUBRA = "https://kubra.io"


def kubra(instance: str, view: str, default_state: str, multistate: bool = False):
    base = f"{KUBRA}/stormcenter/api/v1/stormcenters/{instance}/views/{view}"
    cs = get(base + "/currentState?preview=false")
    data = cs["data"]["interval_generation_data"]
    if multistate:
        cfg = get(f"{base}/configuration/{cs['stormcenterDeploymentId']}?preview=false")
        reports = (((cfg.get("config") or {}).get("reports") or {}).get("data") or {}).get(
            "interval_generation_data") or []
        if reports:
            rep = get(f"{KUBRA}/{data}/{reports[0]['source']}")
            areas = (rep.get("file_data") or {}).get("areas") or []
            states, served = defaultdict(int), 0
            for a in areas:
                name = state_name(a.get("name")) if a.get("key") in ("state", "geopolitical") else None
                states[name or default_state] += num(a.get("cust_a"))
                served += num(a.get("cust_s"))
            if areas:
                return {"states": dict(states), "served": served}
    for n in (1, 2):
        try:
            s = get(f"{KUBRA}/{data}/public/summary-{n}/data.json")
        except RuntimeError:
            continue
        t = s["summaryFileData"]["totals"][0]
        return {"states": {default_state: num(t.get("total_cust_a"))}, "served": num(t.get("total_cust_s"))}
    raise RuntimeError("no Kubra summary file found")


def ifactor_summary(host: str, state: str):
    meta = get(f"https://{host}/resources/data/external/interval_generation_data/metadata.json")
    d = get(f"https://{host}/resources/data/external/interval_generation_data/{meta['directory']}/data.json")
    s = d["summaryFileData"]
    return {"states": {state: num(s.get("total_cust_a"))}, "served": num(s.get("total_cust_s"))}


def ifactor_reports(host: str, reports: dict[str, str], unwrap: bool):
    """reports: {report_file_stem: state}. unwrap=True when the top area is a company wrapper."""
    meta = get(f"https://{host}/resources/data/external/interval_generation_data/metadata.json")
    states, served = defaultdict(int), 0
    for stem, st in reports.items():
        r = get(f"https://{host}/resources/data/external/interval_generation_data/{meta['directory']}/{stem}.json")
        areas = (r.get("file_data") or r).get("areas") or []
        if unwrap:
            areas = [sub for a in areas for sub in (a.get("areas") or [])]
        for a in areas:
            states[st] += num(a.get("cust_a"))
            served += num(a.get("cust_s"))
    return {"states": dict(states), "served": served}


def pge():
    d = get("https://pgealerts-downloads.alerts.pge.com/current_outages/outage-percentages-nonsync.json")
    return {"states": {"California": num(d["total_customers_affected"])}, "served": num(d.get("total_customers"))}


def sce():
    stats = json.dumps([
        {"statisticType": "sum", "onStatisticField": "NoOfAffectedCust_Inci", "outStatisticFieldName": "out"}])
    q = urllib.parse.urlencode({"where": "1=1", "outStatistics": stats, "f": "json"})
    d = get(f"https://sce-outage-ags.esriemcs.com/arcgis/rest/services/43/outage/MapServer/0/query?{q}")
    if "features" not in d:
        raise RuntimeError(f"unexpected ArcGIS reply: {str(d)[:200]}")
    out = d["features"][0]["attributes"].get("out") if d["features"] else 0
    return {"states": {"California": num(out)}, "served": None}


def fpl():
    d = get("https://www.fplmaps.com/customer/outage/CountyOutages.json")
    rows = d["outages"]
    return {"states": {"Florida": sum(num(r.get("Customers Out")) for r in rows)},
            "served": sum(num(r.get("Customers Served")) for r in rows)}


def teco():
    cfg = get("https://outage-data-prod-hrcadje2h9aje9c9.a03.azurefd.net/api/v1/config")
    base = f"https://tecocdn-anc9hvc0bcebdnd3.a03.azurefd.net/{cfg['tileContainer']}/{cfg['index']}"
    man = get(f"{base}/manifest.json")
    tiles = man.get("tiles") or {}
    if not tiles:
        return {"states": {"Florida": 0}, "served": None}
    zmin = min(int(k.split("/")[0]) for k in tiles)
    seen, total = set(), 0
    for key, t in list(tiles.items()):
        if int(key.split("/")[0]) != zmin:
            continue
        tile = get(f"https://tecocdn-anc9hvc0bcebdnd3.a03.azurefd.net/{cfg['tileContainer']}/{t['blob']}")
        for o in tile.get("outages") or []:
            if o.get("incidentId") in seen:
                continue
            seen.add(o.get("incidentId"))
            total += num(o.get("customerCount"))
    return {"states": {"Florida": total}, "served": None}


def datacapable_xcel():
    events = get("https://xcelenergy.datacapable.com/datacapable/v2/cache/p/xcelenergy/map/events")
    states = defaultdict(int)
    for e in events:
        props = {p.get("property"): p.get("value") for p in e.get("additionalProperties") or []}
        sl = props.get("stateList") or [props.get("states")]
        st = state_name(sl[0] if sl else "") or "Minnesota"
        states[st] += num(e.get("numPeople"))
    return {"states": dict(states), "served": None}


def entergy():
    states, served = defaultdict(int), 0
    for region, st in (("la", "Louisiana"), ("nola", "Louisiana"), ("tx", "Texas"),
                       ("ar", "Arkansas"), ("ms", "Mississippi")):
        rows = get(f"https://entergy.datacapable.com/datacapable/v2/p/entergy/r/{region}/map/events/count?types=COUNTY")
        states[st] += sum(num(r.get("customersAffected")) for r in rows)
        served += sum(num(r.get("customersServed")) for r in rows)
    return {"states": dict(states), "served": served}


def ppl():
    d = get("https://api2.pplcloud.net/selfserve/api/outages?opCo=PA")
    return {"states": {"Pennsylvania": num(d.get("customerCount"))}, "served": None}


def pacificorp():
    states = {}
    for host, codes in (("www.pacificpower.net", ("OR", "WA", "CA")), ("www.rockymountainpower.net", ("UT", "ID", "WY"))):
        for c in codes:
            d = get(f"https://{host}/etc/pcorp/datafiles/outagemap/list{c}.json")
            states[ABBR[c]] = num(d.get("totalState"))
    return {"states": states, "served": None}


# name, public map, approx customers served (fallback when the feed lacks it),
# ODIN utility-name regex (to dedupe), states that regex is limited to (None = all), fetch fn
UTILITIES = [
    ("Con Edison", "https://outagemap.coned.com/", 3_600_000, r"CONSOLIDATED EDISON|CON ?ED", None,
     lambda: ifactor_summary("outagemap.coned.com", "New York")),
    ("Orange & Rockland", "https://outagemap.oru.com/", 320_000, r"ORANGE (AND|&) ROCKLAND|ROCKLAND ELEC", None,
     lambda: ifactor_reports("outagemap.oru.com", {"report_ny": "New York", "report_nj": "New Jersey"}, False)),
    ("Eversource", "https://outagemap.eversource.com/", 3_340_000,
     r"EVERSOURCE|CONNECTICUT LIGHT|NSTAR|PUBLIC SERVICE CO OF (NH|NEW HAMPSHIRE)|WESTERN MASSACHUSETTS", None,
     lambda: ifactor_reports("outagemap.eversource.com", {"report_conn": "Connecticut", "report_east": "Massachusetts",
                                                          "report_west": "Massachusetts", "report_hampshire": "New Hampshire"}, True)),
    ("PSEG Long Island", "https://outagemap.psegliny.com/", 1_160_000, r"LONG ISLAND|PSEG LI", None,
     lambda: kubra("48006d55-45bb-40a4-99a6-1289326f0d0d", "fb50e444-70e5-4b69-b868-28463b5dbd19", "New York")),
    ("PSE&G", "https://outagecenter.pseg.com/", 2_400_000, r"PUBLIC SERVICE ELEC", None,
     lambda: kubra("79d82478-c10a-4956-a045-89de3cda618c", "bdb7f5be-1c1a-46fa-862e-ebdf21851d2e", "New Jersey")),
    ("PECO", "https://www.peco.com/outages/experiencing-an-outage/outage-map", 1_700_000, r"\bPECO\b", None,
     lambda: kubra("39e6d9f3-fdea-4539-848f-b8631945da6f", "789577bd-d2c6-42b8-af4b-b51ae6f52b6c", "Pennsylvania")),
    ("BGE", "https://www.bge.com/outages/experiencing-an-outage/outage-map", 1_330_000, r"BALTIMORE GAS", None,
     lambda: kubra("3db5fd35-4a88-465f-8985-8db3eb760b61", "1b43510f-b947-437b-9879-c0bd8f6d7816", "Maryland")),
    ("ComEd", "https://www.comed.com/outages/experiencing-an-outage/outage-map", 4_200_000, r"COMMONWEALTH EDISON|\bCOMED\b", None,
     lambda: kubra("0f46457f-e3ee-473c-8040-d7da9e776ccb", "2e108172-d24f-4c2b-ad49-23a3b1589688", "Illinois")),
    ("PPL Electric", "https://www.pplelectric.com/outages", 1_500_000, r"PPL ELECTRIC", None, ppl),
    ("FirstEnergy (Ohio)", "https://outages.firstenergycorp.com/oh.html", 2_100_000,
     r"OHIO EDISON|CLEVELAND ELEC|TOLEDO EDISON", None,
     lambda: kubra("6c715f0e-bbec-465f-98cc-0b81623744be", "db9c3f02-0a06-4672-a357-0f676eb75bfa", "Ohio", True)),
    ("FirstEnergy (Pennsylvania)", "https://outages.firstenergycorp.com/pa.html", 2_060_000,
     r"METROPOLITAN EDISON|PENNSYLVANIA ELECTRIC|PENNSYLVANIA POWER|WEST PENN", None,
     lambda: kubra("6c715f0e-bbec-465f-98cc-0b81623744be", "8587e451-e258-4692-b5e8-28010506d51a", "Pennsylvania", True)),
    ("FirstEnergy (JCP&L, New Jersey)", "https://outages.firstenergycorp.com/nj.html", 1_160_000, r"JERSEY CENTRAL", None,
     lambda: kubra("6c715f0e-bbec-465f-98cc-0b81623744be", "fcd7c23d-de37-4471-8ca3-37c3260f94fa", "New Jersey", True)),
    ("Dominion Energy", "https://outagemap.dominionenergy.com/", 2_830_000, r"DOMINION|VIRGINIA ELEC", ("Virginia", "North Carolina"),
     lambda: kubra("9c691bb6-767e-4532-b00e-286ac9adc223", "38b5394c-8bca-4dfd-ac59-b321615446bd", "Virginia", True)),
    ("Appalachian Power", "https://outagemap.appalachianpower.com/", 1_050_000, r"APPALACHIAN POWER", None,
     lambda: kubra("6674f49e-0236-4ed8-a40a-b31747557ab7", "8cfe790f-59f3-4ce3-a73f-a9642227411f", "West Virginia", True)),
    ("AEP Ohio", "https://outagemap.aepohio.com/", 1_550_000, r"OHIO POWER|AEP OHIO|COLUMBUS SOUTHERN", None,
     lambda: kubra("9c0735d8-b721-4dce-b80b-558e98ce1083", "9b2feb80-69f8-4035-925e-f2acbcf1728e", "Ohio")),
    ("Indiana Michigan Power", "https://outagemap.indianamichiganpower.com/", 610_000, r"INDIANA MICHIGAN", None,
     lambda: kubra("e07a4ffb-83dd-4251-8dad-3fe5a26979b6", "1b319a36-d1cf-443d-9ecf-6267acae0c3a", "Indiana", True)),
    ("Kentucky Power", "https://outagemap.kentuckypower.com/", 160_000, r"KENTUCKY POWER", None,
     lambda: kubra("23dcd38e-2573-4e20-a463-959b11cae011", "60f31606-5702-4a1e-a74c-08d866b7a6fa", "Kentucky")),
    ("LG&E and KU", "https://stormcenter.lge-ku.com/", 1_040_000, r"LOUISVILLE GAS|KENTUCKY UTILITIES", None,
     lambda: kubra("877fd1e9-4162-473f-b782-d8a53a85326b", "a6cee9e4-312b-4b77-9913-2ae371eb860d", "Kentucky", True)),
    ("DTE Energy", "https://outage.dteenergy.com/map", 2_300_000, r"\bDTE\b|DETROIT EDISON", None,
     lambda: kubra("4fbb3ad3-e01d-4d71-9575-d453769c1171", "8ed2824a-bd92-474e-a7c4-848b812b7f9b", "Michigan")),
    ("Ameren", "https://outagemap.ameren.com/", 2_480_000, r"AMEREN|UNION ELECTRIC", None,
     lambda: kubra("92e209c0-35c3-4ec2-9ce9-30d87a55706f", "9b7c2416-cb0c-4845-99c7-7b31041038a2", "Missouri", True)),
    ("Evergy", "https://outagemap.evergy.com/", 1_710_000, r"EVERGY|KANSAS CITY POWER|WESTAR", None,
     lambda: kubra("b1493825-4ee3-4706-a986-99a763a733db", "c1062d22-2919-487c-9000-e21b72b62278", "Kansas", True)),
    ("Xcel Energy", "https://outagemap.xcelenergy.com/", 3_900_000,
     r"XCEL|NORTHERN STATES POWER|PUBLIC SERVICE CO OF COLORADO|SOUTHWESTERN PUBLIC SERVICE", None, datacapable_xcel),
    ("Georgia Power", "https://outagemap.georgiapower.com/", 2_700_000, r"GEORGIA POWER", None,
     lambda: kubra("7b38c047-7950-444b-a25c-9b3e5ab986eb", "67b44af5-3847-4ca3-9f4e-9190aac343d6", "Georgia")),
    ("Alabama Power", "https://outagemap.alabamapower.com/", 1_500_000, r"ALABAMA POWER", None,
     lambda: kubra("7636a60f-7b81-4fb0-a30d-ed79a8e271e7", "c3471c92-6e1b-494b-a884-391139a2cc18", "Alabama")),
    ("Florida Power & Light", "https://www.fplmaps.com/", 6_000_000, r"FLORIDA POWER (&|AND) LIGHT|\bFPL\b|GULF POWER", None, fpl),
    ("Tampa Electric", "https://outage.tecoenergy.com/", 830_000, r"TAMPA ELEC", None, teco),
    ("Entergy", "https://www.etrviewoutage.com/map", 3_000_000, r"ENTERGY", None, entergy),
    ("SWEPCO", "https://outagemap.swepco.com/", 550_000, r"SOUTHWESTERN ELECTRIC POWER|SWEPCO", None,
     lambda: kubra("9632df6e-a385-400a-b822-b68d268e8e7c", "e51304d0-7052-458d-a5ac-0e1980cca976", "Louisiana", True)),
    ("PSO", "https://outagemap.psoklahoma.com/", 580_000, r"PUBLIC SERVICE CO OF OKLAHOMA", None,
     lambda: kubra("4bb3b3bc-e1c4-448b-b806-e4fc85c3b640", "e2356e43-c76f-4772-bf85-31240a2cc504", "Oklahoma")),
    ("Oncor", "https://stormcenter.oncor.com/", 4_000_000, r"ONCOR", None,
     lambda: kubra("560abba3-7881-4741-b538-ca416b58ba1e", "ca124b24-9a06-4b19-aeb3-1841a9c962e1", "Texas")),
    ("AEP Texas", "https://outagemap.aeptexas.com/", 1_100_000, r"AEP TEXAS", None,
     lambda: kubra("3ff6812b-90d8-40cd-97a6-76633226f27b", "6022fe09-5259-4763-892f-5f57463fa6a5", "Texas")),
    ("CPS Energy", "https://outagemap.cpsenergy.com/", 900_000, r"CPS ENERGY|CITY OF SAN ANTONIO", None,
     lambda: kubra("912c6202-c3f4-491c-a8c1-726157725e92", "812092c0-153f-4a7f-8c58-e1af1cb740b7", "Texas")),
    ("Austin Energy", "https://outagemap.austinenergy.com/", 550_000, r"AUSTIN ENERGY|CITY OF AUSTIN", None,
     lambda: kubra("dd9c446f-f6b8-43f9-8f80-83f5245c60a1", "76446308-a901-4fa3-849c-3dd569933a51", "Texas")),
    ("PG&E", "https://pgealerts.alerts.pge.com/outagecenter/", 5_700_000, r"PACIFIC GAS", None, pge),
    ("Southern California Edison", "https://www.sce.com/outage-center/check-outage-status", 5_200_000,
     r"SOUTHERN CALIFORNIA EDISON", None, sce),
    ("SDG&E", "https://www.sdge.com/residential/customer-service/outage-center/outage-map", 1_480_000, r"SAN DIEGO GAS", None,
     lambda: kubra("baefd37d-17f9-4bb7-befb-f2f2b38b8cb8", "031b05f7-e0fe-4bd0-98fa-a70f017e8a5e", "California")),
    ("PacifiCorp (Pacific Power / Rocky Mountain Power)", "https://www.pacificpower.net/outages-safety.html", 2_100_000,
     r"PACIFICORP|ROCKY MOUNTAIN POWER|PACIFIC POWER", None, pacificorp),
]

# Large utilities deliberately NOT fetched directly (ODIN is used for them when it has them).
SKIPPED = [
    {"utility": "Duke Energy (NC/SC/FL/IN/OH/KY)", "reason": "outage-map API requires an Authorization header (private API key); not a public feed. Not in ODIN either."},
    {"utility": "Consumers Energy (MI)", "reason": "robots.txt disallows /arcgispublic/* (the map's data path). Covered by ODIN."},
    {"utility": "CenterPoint Energy (TX/IN)", "reason": "public map feed is stale/not live; covered by ODIN."},
    {"utility": "National Grid (NY/MA)", "reason": "outage map host refuses non-browser connections; covered by ODIN."},
    {"utility": "NYSEG / RG&E (Avangrid)", "reason": "API gateway refuses non-browser connections; covered by ODIN (AVANGRID)."},
    {"utility": "Pepco / Delmarva / Atlantic City Electric", "reason": "map not found in this pass; not in ODIN (gap)."},
    {"utility": "APS, Duquesne Light, Puget Sound Energy, Portland General, MidAmerican, Central Hudson, Hawaiian Electric",
     "reason": "already report to ODIN; no separate adapter needed."},
    {"utility": "LUMA (Puerto Rico), SRP, NV Energy, LADWP, SMUD", "reason": "no stable public JSON found in this pass (gap)."},
]


def run_adapter(u):
    name, url, served_fallback, pattern, limit_states, fn = u
    t0 = time.time()
    try:
        res = fn()
        states = {k: int(v) for k, v in res["states"].items() if k}
        return {"utility": name, "ok": True, "map": url, "customers_out": sum(states.values()),
                "by_state": dict(sorted(states.items(), key=lambda kv: -kv[1])),
                "served": res.get("served") or served_fallback, "served_is_estimate": not res.get("served"),
                "seconds": round(time.time() - t0, 1), "_pattern": pattern, "_limit": limit_states}
    except Exception as exc:  # noqa: BLE001 - one broken adapter must not fail the run
        return {"utility": name, "ok": False, "map": url, "error": str(exc)[:300],
                "seconds": round(time.time() - t0, 1), "_pattern": pattern, "_limit": limit_states}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="shadow/combined_us.json")
    ap.add_argument("--log", default="shadow/combined_log.csv")
    ap.add_argument("--compare", default="data.json", help="data.json to compare against (read only)")
    args = ap.parse_args()

    now = datetime.now(timezone.utc).replace(microsecond=0)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(run_adapter, UTILITIES))

    try:
        records, odin_source = odin.load_records()
        odin_error = None
    except Exception as exc:  # noqa: BLE001
        records, odin_source, odin_error = [], None, str(exc)[:300]

    ok = [r for r in results if r["ok"]]
    claims = [(re.compile(r["_pattern"], re.I), set(r["by_state"]) | set(r["_limit"] or []), r["_limit"]) for r in ok]

    by_state = defaultdict(lambda: {"direct": 0, "odin": 0, "top_src": ("", 0)})
    for r in ok:
        for st, n in r["by_state"].items():
            s = by_state[st]
            s["direct"] += n
            if n > s["top_src"][1]:
                s["top_src"] = (r["map"], n)

    odin_only = defaultdict(int)
    odin_extra_util = defaultdict(int)
    deduped = 0
    for rec in records:
        code = rec["fips"][:2]
        if code not in odin.STATES:
            continue
        st = odin.STATES[code][0]
        odin_only[st] += rec["customers"]
        claimed = any(rx.search(rec["utility"]) and (lim is None or st in lim) for rx, _states, lim in claims)
        if claimed:
            deduped += rec["customers"]
            continue
        by_state[st]["odin"] += rec["customers"]
        odin_extra_util[rec["utility"]] += rec["customers"]
        if rec["customers"] > by_state[st]["top_src"][1] and not by_state[st]["direct"]:
            by_state[st]["top_src"] = (odin.PUBLIC_SOURCE, rec["customers"])

    states = sorted(
        ({"state": st, "customers_out": v["direct"] + v["odin"], "direct_maps": v["direct"], "odin_extra": v["odin"],
          "source": v["top_src"][0] or odin.PUBLIC_SOURCE}
         for st, v in by_state.items() if v["direct"] + v["odin"] > 0),
        key=lambda s: (-s["customers_out"], s["state"]))
    total = sum(s["customers_out"] for s in states)
    direct_total = sum(r["customers_out"] for r in ok)
    odin_extra_total = sum(v["odin"] for v in by_state.values())
    odin_only_total = sum(odin_only.values())
    served = sum(r["served"] for r in ok)
    coverage = round(100 * served / US_CUSTOMERS, 1)

    updated = odin.fmt_ct(now)
    fips_by_name = {v[0]: k for k, v in odin.STATES.items()}
    rows = [{"lat": None, "lon": None, "place": "United States", "region": "Nationwide", "cause": "",
             "affected": f"{total:,} customers out", "start": "", "etr": "", "source": odin.PUBLIC_SOURCE,
             "updated": updated}]
    for s in states[:TOP_N]:
        _, lat, lon = odin.STATES[fips_by_name[s["state"]]]
        rows.append({"lat": lat, "lon": lon, "place": s["state"], "region": "United States", "cause": "",
                     "affected": f"{s['customers_out']:,} customers out", "start": "", "etr": "",
                     "source": s["source"], "updated": updated})

    pous = odin.poweroutage_us_snapshot(Path(args.compare)) if args.compare else None
    for r in results:
        r.pop("_pattern", None)
        r.pop("_limit", None)

    result = {
        "generated_at_utc": now.isoformat().replace("+00:00", "Z"),
        "generated_at_ct": now.astimezone(odin.CT).strftime("%-I:%M %p CT, %A, %B %-d, %Y"),
        "mode": "shadow (not used by the live site)",
        "method": "direct utility outage maps for covered utilities + ODIN for everyone else; a utility in both is counted once (direct map preferred)",
        "total": total,
        "direct_maps_total": direct_total,
        "odin_extra_total": odin_extra_total,
        "odin_only_total": odin_only_total,
        "odin_customers_deduped": deduped,
        "odin_source": odin_source,
        "odin_error": odin_error,
        "adapters_ok": len(ok),
        "adapters_failed": len(results) - len(ok),
        "direct_coverage_customers_served": served,
        "direct_coverage_pct_of_us_customers": coverage,
        "states": states,
        "rows": rows,
        "utilities": sorted(results, key=lambda r: -(r.get("customers_out") or 0)),
        "failures": [{"utility": r["utility"], "error": r["error"]} for r in results if not r["ok"]],
        "odin_extra_by_utility": dict(sorted(odin_extra_util.items(), key=lambda kv: -kv[1])[:40]),
        "skipped": SKIPPED,
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
            w.writerow(["timestamp_utc", "combined_total", "combined_top5", "direct_maps_total", "odin_extra_total",
                        "odin_only_total", "adapters_ok", "adapters_failed", "coverage_pct",
                        "pous_as_of", "pous_total", "pous_top5"])
        w.writerow([
            result["generated_at_utc"], total,
            odin.top5_str((s["state"], s["customers_out"]) for s in states[:TOP_N]),
            direct_total, odin_extra_total, odin_only_total, len(ok), len(results) - len(ok), coverage,
            (pous or {}).get("as_of", ""), (pous or {}).get("total", ""), odin.top5_str((pous or {}).get("top5", [])),
        ])

    print(f"Combined total {total:,} (direct maps {direct_total:,} + ODIN extra {odin_extra_total:,}); "
          f"ODIN alone {odin_only_total:,}. Adapters ok {len(ok)}/{len(results)}; direct coverage ~{coverage}%.")
    for s in states[:TOP_N]:
        print(f"  {s['state']}: {s['customers_out']:,} (maps {s['direct_maps']:,}, ODIN {s['odin_extra']:,})")
    for f in result["failures"]:
        print(f"  FAILED {f['utility']}: {f['error']}")
    if pous:
        print(f"poweroutage.us (data.json, {pous['as_of']}): total {pous['total']:,}; {odin.top5_str(pous['top5'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
