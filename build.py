#!/usr/bin/env python3
"""Generate crawlable HTML, region pages, sitemap, and the social share image.

Run this after every data.json update, before committing:

    python3 build.py

The live page still fetches data.json in the browser and refreshes every
five minutes. This script bakes the current snapshot into static HTML so
search engines can read the outages without JavaScript.
"""

from __future__ import annotations

import html
import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
DATA_PATH = ROOT / "data.json"
INDEX_PATH = ROOT / "index.html"
OG_IMAGE_PATH = ROOT / "og-image.png"
FAVICON_PATH = ROOT / "favicon.png"
SITEMAP_PATH = ROOT / "sitemap.xml"
ROBOTS_PATH = ROOT / "robots.txt"
GENERATED_MARKER = ".generated"

# Public site URL (GitHub Pages project site). Change this if a custom domain is added.
SITE_BASE = "https://marcustravis.github.io/power-outage-map"

HOME_TITLE = "Live Power Outage Map: Major Blackouts Worldwide Right Now"

SOURCE_LABELS = {
    "poweroutage.us": "PowerOutage.us",
    "cubadebate.cu": "Cubadebate / UNE",
    "ua.news": "UA.NEWS",
    "obozrevatel.com": "OBOZ.UA / Ukrenergo",
    "washingtonpost.com": "The Washington Post",
    "thursdaytimes.com": "The Thursday Times",
    "abc.net.au": "ABC News",
    "energex.com.au": "Energex",
    "sabcnews.com": "SABC News",
    "vislandguide.com": "VIsland Guide (WAPA map)",
    "viwapa.vi": "WAPA",
    "vanguardngr.com": "Vanguard",
}

# Primary country/region pages. A row can also appear on a related page (e.g. USVI on /us/).
PAGE_INFO = {
    "us": {
        "name": "the United States",
        "heading": "US Power Outage Today",
        "title": "US Power Outage Today | Live Power Outage Map",
        "link": "US power outage today",
        "also_include_slugs": {"us-virgin-islands"},
    },
    "us-virgin-islands": {
        "name": "the U.S. Virgin Islands",
        "heading": "US Virgin Islands Power Outage Today",
        "title": "US Virgin Islands Power Outage Today | Live Power Outage Map",
        "link": "US Virgin Islands power outage today",
    },
    "ukraine": {
        "name": "Ukraine",
        "heading": "Ukraine Power Outage Today",
        "title": "Ukraine Power Outage Today | Live Power Outage Map",
        "link": "Ukraine power outage today",
    },
    "cuba": {
        "name": "Cuba",
        "heading": "Cuba Power Outage Today",
        "title": "Cuba Power Outage Today | Live Power Outage Map",
        "link": "Cuba power outage today",
    },
    "venezuela": {
        "name": "Venezuela",
        "heading": "Venezuela Power Outage Today",
        "title": "Venezuela Power Outage Today | Live Power Outage Map",
        "link": "Venezuela power outage today",
    },
    "pakistan": {
        "name": "Pakistan",
        "heading": "Pakistan Power Outage Today",
        "title": "Pakistan Power Outage Today | Live Power Outage Map",
        "link": "Pakistan power outage today",
    },
    "australia": {
        "name": "Australia",
        "heading": "Australia Power Outage Today",
        "title": "Australia Power Outage Today | Live Power Outage Map",
        "link": "Australia power outage today",
    },
    "south-africa": {
        "name": "South Africa",
        "heading": "South Africa Power Outage Today",
        "title": "South Africa Power Outage Today | Live Power Outage Map",
        "link": "South Africa power outage today",
    },
    "nigeria": {
        "name": "Nigeria",
        "heading": "Nigeria Power Outage Today",
        "title": "Nigeria Power Outage Today | Live Power Outage Map",
        "link": "Nigeria power outage today",
    },
}

# Display order for the homepage country links.
LINK_ORDER = [
    "us",
    "ukraine",
    "cuba",
    "venezuela",
    "pakistan",
    "australia",
    "south-africa",
    "nigeria",
    "us-virgin-islands",
]


def esc(value: str | None) -> str:
    return html.escape((value or "").strip(), quote=True)


def source_host(url: str) -> str:
    try:
        from urllib.parse import urlparse

        return urlparse(url).hostname.replace("www.", "") if urlparse(url).hostname else ""
    except Exception:
        return ""


def source_label(url: str) -> str:
    host = source_host(url)
    return SOURCE_LABELS.get(host) or host or url or ""


def dash(value: str | None) -> str:
    value = (value or "").strip()
    return value if value else "—"


def classify(row: dict) -> str | None:
    """Return the primary region-page slug for a row, or None."""
    place = (row.get("place") or "").strip()
    region = (row.get("region") or "").strip()
    blob = f"{place} {region}".lower()

    if "virgin islands" in blob:
        return "us-virgin-islands"
    if region == "United States" or place == "United States":
        return "us"
    if "ukraine" in blob:
        return "ukraine"
    if "cuba" in blob:
        return "cuba"
    if "venezuela" in blob:
        return "venezuela"
    if "pakistan" in blob:
        return "pakistan"
    if "australia" in blob:
        return "australia"
    if "south africa" in blob or "johannesburg" in blob:
        return "south-africa"
    if "nigeria" in blob:
        return "nigeria"
    return None


def row_key(row: dict) -> str:
    return f"{row.get('place') or ''}|{row.get('region') or ''}"


def parse_as_of(as_of: str) -> datetime | None:
    """Parse '5:37 PM CT, Thursday, November 12, 2026' as America/Chicago."""
    match = re.search(
        r"(\d{1,2}:\d{2}\s*[AP]M)\s*CT,\s*[A-Za-z]+,\s*([A-Za-z]+ \d{1,2}, \d{4})",
        as_of or "",
    )
    if not match:
        return None
    try:
        naive = datetime.strptime(f"{match.group(2)} {match.group(1)}", "%B %d, %Y %I:%M %p")
        return naive.replace(tzinfo=ZoneInfo("America/Chicago"))
    except ValueError:
        return None


def isoformat_as_of(as_of: str) -> str:
    parsed = parse_as_of(as_of)
    if parsed:
        return parsed.isoformat()
    return datetime.now(ZoneInfo("America/Chicago")).isoformat()


def home_description(as_of: str, rows: list[dict]) -> str:
    places = []
    for row in rows:
        place = (row.get("place") or "").strip()
        slug = classify(row)
        # Prefer country-level names in the description.
        if slug == "us" and place and place != "United States":
            continue
        if place and place not in places:
            places.append(place)
        if len(places) >= 6:
            break
    where = ", ".join(places[:6]) if places else "major blackouts worldwide"
    return (
        f"Live unofficial map of significant power outages worldwide as of {as_of}. "
        f"Current outages include {where}. Customers affected, causes, and restoration times, with sources. Ad-free."
    )


def short_description(as_of: str, name: str, rows: list[dict]) -> str:
    bits = []
    for row in rows:
        affected = (row.get("affected") or "").strip()
        if affected:
            bits.append(f"{row.get('place')}: {affected}")
        if len(bits) >= 2:
            break
    detail = " ".join(bits)
    base = f"Current power outages in {name} as of {as_of}."
    if detail:
        # Keep meta descriptions reasonably short.
        combined = f"{base} {detail}"
        if len(combined) > 220:
            combined = combined[:217].rstrip() + "…"
        return combined + " Unofficial snapshot with sources."
    return f"{base} Unofficial snapshot of significant outages, with sources. Ad-free."


def replace_block(text: str, name: str, inner: str) -> str:
    pairs = [
        (f"<!-- build:{name} -->", f"<!-- /build:{name} -->"),
        (f"/* build:{name} */", f"/* /build:{name} */"),
    ]
    for start, end in pairs:
        pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)
        if pattern.search(text):
            return pattern.sub(start + inner + end, text, count=1)
    raise SystemExit(f"Missing build markers for {name}")


def table_rows_html(rows: list[dict], hrefs: dict[str, str], href_prefix: str = "") -> str:
    out = []
    for row in rows:
        href = hrefs.get(row_key(row), "")
        place = dash(row.get("place"))
        if href and place != "—":
            place_html = f'<a class="place-link" href="{esc(href_prefix + href)}">{esc(place)}</a>'
        else:
            place_html = esc(place)
        source = (row.get("source") or "").strip()
        if source:
            source_html = (
                f'<a href="{esc(source)}" target="_blank" rel="noopener noreferrer">'
                f"{esc(source_label(source))}</a>"
            )
        else:
            source_html = '<span class="empty">—</span>'

        def cell(value: str | None) -> str:
            text = (value or "").strip()
            if not text:
                return '<td class="empty">—</td>'
            return f"<td>{esc(text)}</td>"

        out.append(
            "<tr>"
            f'<td class="place-cell">{place_html}</td>'
            f"{cell(row.get('region'))}"
            f"{cell(row.get('cause'))}"
            f"{cell(row.get('affected'))}"
            f"{cell(row.get('start'))}"
            f"{cell(row.get('etr'))}"
            f"<td>{source_html}</td>"
            f"{cell(row.get('updated'))}"
            "</tr>"
        )
    return "\n".join(out)


def metric_us_html(rows: list[dict]) -> str:
    us = next((row for row in rows if (row.get("place") or "") == "United States"), None)
    if not us:
        return ""
    affected = (us.get("affected") or "").strip()
    # First number phrase, matching the page's JS.
    match = re.search(r"[\d,]+(?:\.\d+)?(?:\s*MW)?", affected)
    figure = match.group(0) if match else "—"
    return (
        f'<div class="label">United States</div>'
        f'<div class="value">{esc(figure)}</div>'
        f'<div class="detail">{esc(affected)}</div>'
    )


def sources_html(rows: list[dict]) -> str:
    seen: set[str] = set()
    items = []
    for row in rows:
        url = (row.get("source") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        items.append(
            f'<li><a href="{esc(url)}" target="_blank" rel="noopener noreferrer">'
            f"{esc(source_label(url))}</a></li>"
        )
    return "\n".join(items)


def region_links_html(slugs: list[str]) -> str:
    items = []
    for slug in slugs:
        info = PAGE_INFO[slug]
        items.append(f'<li><a href="{esc(slug)}/">{esc(info["link"])}</a></li>')
    return "\n".join(items)


def json_ld_home(as_of: str, description: str, rows: list[dict]) -> str:
    places = [(row.get("place") or "").strip() for row in rows if (row.get("place") or "").strip()]
    data = {
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "WebSite",
                "name": "Live Power Outage Map",
                "url": SITE_BASE + "/",
                "description": description,
                "inLanguage": "en",
            },
            {
                "@type": "Dataset",
                "name": "Significant power outages worldwide",
                "description": description,
                "url": SITE_BASE + "/",
                "isAccessibleForFree": True,
                "license": "https://creativecommons.org/licenses/by/4.0/",
                "dateModified": isoformat_as_of(as_of),
                "temporalCoverage": isoformat_as_of(as_of),
                "spatialCoverage": {"@type": "Place", "name": "Worldwide"},
                "creator": {"@type": "Organization", "name": "Live Power Outage Map"},
                "distribution": {
                    "@type": "DataDownload",
                    "encodingFormat": "application/json",
                    "contentUrl": SITE_BASE + "/data.json",
                },
                "variableMeasured": "Power outages and generation shortfalls",
                "keywords": places,
            },
        ],
    }
    return json.dumps(data, ensure_ascii=False, indent=2)


def json_ld_region(slug: str, as_of: str, description: str, info: dict) -> str:
    url = f"{SITE_BASE}/{slug}/"
    data = {
        "@context": "https://schema.org",
        "@type": "WebPage",
        "name": info["heading"],
        "url": url,
        "description": description,
        "isPartOf": {"@type": "WebSite", "name": "Live Power Outage Map", "url": SITE_BASE + "/"},
        "dateModified": isoformat_as_of(as_of),
        "about": {"@type": "Dataset", "name": f"Significant power outages in {info['name']}", "url": url},
    }
    return json.dumps(data, ensure_ascii=False, indent=2)


def seo_head(
    title: str,
    description: str,
    canonical: str,
    json_ld: str,
    stylesheet: str,
    extra_links: str = "",
) -> str:
    image = SITE_BASE + "/og-image.png"
    return "\n".join(
        [
            f"  <title>{esc(title)}</title>",
            f'  <meta name="description" content="{esc(description)}">',
            '  <meta name="robots" content="index,follow">',
            f'  <link rel="canonical" href="{esc(canonical)}">',
            f'  <link rel="icon" type="image/png" href="{esc(SITE_BASE + "/favicon.png")}">',
            f'  <link rel="stylesheet" href="{esc(stylesheet)}">',
            '  <meta property="og:type" content="website">',
            f'  <meta property="og:title" content="{esc(title)}">',
            f'  <meta property="og:description" content="{esc(description)}">',
            f'  <meta property="og:url" content="{esc(canonical)}">',
            '  <meta property="og:site_name" content="Live Power Outage Map">',
            '  <meta property="og:locale" content="en_US">',
            f'  <meta property="og:image" content="{esc(image)}">',
            '  <meta property="og:image:type" content="image/png">',
            '  <meta property="og:image:width" content="1200">',
            '  <meta property="og:image:height" content="630">',
            '  <meta property="og:image:alt" content="Live Power Outage Map: major blackouts worldwide right now">',
            '  <meta name="twitter:card" content="summary_large_image">',
            f'  <meta name="twitter:title" content="{esc(title)}">',
            f'  <meta name="twitter:description" content="{esc(description)}">',
            f'  <meta name="twitter:image" content="{esc(image)}">',
            extra_links,
            '  <script type="application/ld+json">',
            json_ld,
            "  </script>",
        ]
    )


def patch_index(as_of: str, rows: list[dict], hrefs: dict[str, str], slugs: list[str]) -> None:
    text = INDEX_PATH.read_text(encoding="utf-8")
    description = home_description(as_of, rows)
    canonical = SITE_BASE + "/"
    head = seo_head(
        HOME_TITLE,
        description,
        canonical,
        json_ld_home(as_of, description, rows),
        "site.css",
        extra_links='  <link rel="alternate" type="application/json" href="data.json" title="Outage data (JSON)">',
    )
    text = replace_block(text, "seo-head", "\n" + head + "\n  ")
    text = re.sub(
        r'(<strong id="as-of">)(.*?)(</strong>)',
        lambda m: m.group(1) + esc(as_of) + m.group(3),
        text,
        count=1,
        flags=re.DOTALL,
    )
    metric = metric_us_html(rows)
    text = replace_block(text, "metric-us", metric)
    if metric:
        text = text.replace('<div class="metrics" id="metrics" hidden>', '<div class="metrics" id="metrics">', 1)
    else:
        text = text.replace('<div class="metrics" id="metrics">', '<div class="metrics" id="metrics" hidden>', 1)
    text = replace_block(text, "region-links", "\n      " + region_links_html(slugs) + "\n      ")
    text = replace_block(text, "table", "\n" + table_rows_html(rows, hrefs) + "\n          ")
    text = replace_block(text, "sources", "\n      " + sources_html(rows) + "\n      ")
    hrefs_js = "    const REGION_HREFS = " + json.dumps(hrefs, ensure_ascii=False, indent=6) + ";"
    # json.dumps indent leaves the closing brace flush; keep it as a JS const assignment.
    text = replace_block(text, "region-hrefs", "\n" + hrefs_js + "\n    ")
    INDEX_PATH.write_text(text, encoding="utf-8")


def kofi_html() -> str:
    return (
        '<p class="kofi">Free and ad-free. If this site helped you, '
        '<a href="https://ko-fi.com/marcustravis" target="_blank" rel="noopener">buy me a coffee →</a></p>'
    )


def analytics_html() -> str:
    return (
        "<!-- Cloudflare Web Analytics -->"
        "<script type='module' src='https://static.cloudflareinsights.com/beacon.min.js' "
        "data-cf-beacon='{\"token\": \"7234bf51ba3b43458e810475e5d79c30\"}'></script>"
        "<!-- End Cloudflare Web Analytics -->"
    )


def write_region_page(slug: str, as_of: str, rows: list[dict], hrefs: dict[str, str], info: dict) -> None:
    directory = ROOT / slug
    directory.mkdir(exist_ok=True)
    (directory / GENERATED_MARKER).write_text("generated by build.py; do not edit\n", encoding="utf-8")
    description = short_description(as_of, info["name"], rows)
    canonical = f"{SITE_BASE}/{slug}/"
    head = seo_head(
        info["title"],
        description,
        canonical,
        json_ld_region(slug, as_of, description, info),
        "../site.css",
    )
    table = table_rows_html(rows, hrefs, href_prefix="../")
    # Place links on a region page should stay on this page or go to a sibling; hrefs are like "ukraine/".
    # Prefix with ../ so they resolve from /ukraine/ to /ukraine/ or /us/.
    sources = sources_html(rows)
    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
{head}
</head>
<body>
  <div class="wrap">
    <p class="back-link"><a href="../">← Live worldwide power outage map</a></p>
    <header class="top">
      <h1>{esc(info["heading"])}</h1>
      <p class="dateline">As of <strong>{esc(as_of)}</strong><br>Times in Central Time (CT)</p>
    </header>
    <p class="region-lede">Unofficial snapshot of significant power outages in {esc(info["name"])}. Figures come from public utility and news reports and are not invented. Confirm any outage with the local utility. For the latest worldwide list, see the <a href="../">live power outage map</a>.</p>
    <section class="panel" aria-labelledby="table-heading">
      <h2 id="table-heading">Current outages</h2>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Place</th>
              <th>Region</th>
              <th>Cause</th>
              <th>Affected</th>
              <th>Start</th>
              <th>ETR</th>
              <th>Source</th>
              <th>Updated</th>
            </tr>
          </thead>
          <tbody>
{table}
          </tbody>
        </table>
      </div>
    </section>
    <footer>
      <h2>Sources</h2>
      <ul>
{sources}
      </ul>
      <p>This is an unofficial snapshot compiled from public utility and news reports. It is not an official government or utility outage map. Confirm outages with the local utility.</p>
    </footer>
    {kofi_html()}
  </div>
{analytics_html()}
</body>
</html>
"""
    (directory / "index.html").write_text(page, encoding="utf-8")


def clean_stale_pages(keep: set[str]) -> None:
    for path in ROOT.iterdir():
        if not path.is_dir() or path.name.startswith("."):
            continue
        marker = path / GENERATED_MARKER
        if marker.is_file() and path.name not in keep:
            shutil.rmtree(path)


def write_robots() -> None:
    ROBOTS_PATH.write_text(
        "User-agent: *\n"
        "Allow: /\n"
        f"Sitemap: {SITE_BASE}/sitemap.xml\n",
        encoding="utf-8",
    )


def write_sitemap(as_of: str, slugs: list[str]) -> None:
    lastmod = isoformat_as_of(as_of)
    urls = [SITE_BASE + "/"] + [f"{SITE_BASE}/{slug}/" for slug in slugs]
    items = []
    for i, url in enumerate(urls):
        priority = "1.0" if i == 0 else "0.8"
        changefreq = "hourly" if i == 0 else "daily"
        items.append(
            "  <url>\n"
            f"    <loc>{esc(url)}</loc>\n"
            f"    <lastmod>{esc(lastmod)}</lastmod>\n"
            f"    <changefreq>{changefreq}</changefreq>\n"
            f"    <priority>{priority}</priority>\n"
            "  </url>"
        )
    SITEMAP_PATH.write_text(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<urlset xmlns=\"http://www.sitemaps.org/schemas/sitemap/0.9\">\n"
        + "\n".join(items)
        + "\n</urlset>\n",
        encoding="utf-8",
    )


def _load_font(paths: list[str], size: int):
    from PIL import ImageFont

    for path in paths:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def write_images(as_of: str, rows: list[dict]) -> None:
    from PIL import Image, ImageDraw

    serif_bold = [
        "/usr/share/fonts/truetype/sand-box/google/IBM Plex Serif/IBMPlexSerif-SemiBold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
    ]
    serif = [
        "/usr/share/fonts/truetype/sand-box/google/IBM Plex Serif/IBMPlexSerif-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    ]
    sans = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    title_font = _load_font(serif_bold, 64)
    subtitle_font = _load_font(serif, 32)
    meta_font = _load_font(sans, 24)
    chip_font = _load_font(sans, 22)

    width, height = 1200, 630
    image = Image.new("RGB", (width, height), "#f3efe6")
    # Soft warm wash in the upper left, matching the site.
    wash = Image.new("RGB", (width, height), "#f3efe6")
    wdraw = ImageDraw.Draw(wash)
    wdraw.ellipse((-180, -200, 720, 460), fill="#fff8e8")
    image = Image.blend(image, wash, 0.85)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 14, height), fill="#9b1c1c")

    left = 72
    draw.text((left, 88), "Live Power Outage Map", font=title_font, fill="#1c1915")
    draw.text((left, 172), "Major blackouts worldwide right now", font=subtitle_font, fill="#5c574c")
    draw.text((left, 230), f"As of {as_of}", font=meta_font, fill="#5c574c")

    # Location chips from the current snapshot (worldwide only).
    labels = []
    seen = set()
    for row in rows:
        slug = classify(row)
        if not slug:
            continue
        # Use the country page heading without "Power Outage Today"
        name = PAGE_INFO[slug]["link"].replace(" power outage today", "")
        if name == "US":
            name = "United States"
        if name in seen:
            continue
        seen.add(name)
        labels.append(name)
        if len(labels) >= 6:
            break

    x, y = left, 320
    max_x = width - 72
    for label in labels:
        text_w = draw.textlength(label, font=chip_font)
        box_w = int(text_w + 52)
        box_h = 44
        if x + box_w > max_x:
            x = left
            y += 56
        draw.rounded_rectangle((x, y, x + box_w, y + box_h), radius=22, fill="#fffcf6", outline="#d4cdc0")
        draw.ellipse((x + 14, y + 16, x + 26, y + 28), fill="#9b1c1c")
        draw.text((x + 36, y + 10), label, font=chip_font, fill="#1c1915")
        x += box_w + 12

    draw.text((left, 560), "Unofficial  ·  Ad-free  ·  Sources on every row", font=meta_font, fill="#5c574c")
    image.save(OG_IMAGE_PATH, "PNG", optimize=True)

    # Simple tab icon: cream square, red alert dot.
    icon = Image.new("RGB", (64, 64), "#f3efe6")
    idraw = ImageDraw.Draw(icon)
    idraw.ellipse((16, 16, 48, 48), fill="#9b1c1c")
    icon.save(FAVICON_PATH, "PNG", optimize=True)


def rows_for_slug(slug: str, rows: list[dict]) -> list[dict]:
    info = PAGE_INFO[slug]
    extras = info.get("also_include_slugs", set())
    selected = []
    for row in rows:
        row_slug = classify(row)
        if row_slug == slug or row_slug in extras:
            selected.append(row)
    return selected


def used_slugs(rows: list[dict]) -> list[str]:
    present = {classify(row) for row in rows}
    present.discard(None)
    # Include /us/ if only a related territory is present? Only if US or USVI rows exist.
    if "us-virgin-islands" in present:
        present.add("us")
    ordered = [slug for slug in LINK_ORDER if slug in present]
    for slug in sorted(present):
        if slug not in ordered and slug in PAGE_INFO:
            ordered.append(slug)
    return ordered


def main() -> None:
    data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    as_of = (data.get("as_of") or "").strip() or "time not provided in data.json"
    rows = data.get("outages") if isinstance(data.get("outages"), list) else []
    slugs = used_slugs(rows)
    hrefs = {}
    for row in rows:
        slug = classify(row)
        if slug:
            hrefs[row_key(row)] = f"{slug}/"

    patch_index(as_of, rows, hrefs, slugs)
    clean_stale_pages(set(slugs))
    for slug in slugs:
        write_region_page(slug, as_of, rows_for_slug(slug, rows), hrefs, PAGE_INFO[slug])
    write_robots()
    write_sitemap(as_of, slugs)
    write_images(as_of, rows)
    print(f"Built homepage, {len(slugs)} region pages, sitemap, and share image.")
    print("Region pages: " + ", ".join(f"/{s}/" for s in slugs))


if __name__ == "__main__":
    main()
