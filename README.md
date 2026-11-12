# Power Outage Map

An unofficial static site that plots significant power outages on an [OpenStreetMap](https://www.openstreetmap.org/copyright) basemap and lists them in a sortable table.

This is not an official government or utility outage map. Confirm any outage with the local utility.

## Files

- `index.html` — page, styles, and Leaflet map. Do not put outage figures in this file.
- `data.json` — the only file that should change on each refresh. It holds the `as_of` timestamp and the `outages` array.
- `README.md` — this file.

## Updating the data

Edit `data.json` only. The page fetches that file with a cache-busting query string and reloads it every five minutes, so new numbers appear without changing the HTML.

Refresh cadence for the data itself:

- United States: every 30 minutes
- Worldwide list: a few times a day (about four)

Set `as_of` to the snapshot time in Central Time (CT), for example `"2:35 PM CT, Thursday, November 12, 2026"`.

Each object in `outages` should use these fields (leave a value blank when it is unknown — do not invent numbers):

| Field | Meaning |
| --- | --- |
| `lat`, `lon` | Approximate coordinates. Use `null` for nationwide totals that should appear in the table but not as a single map dot. |
| `place` | Location name |
| `region` | Broader area or country |
| `cause` | Why the outage or shortfall is happening |
| `affected` | Customers out, MW shortfall, or a short qualitative note |
| `start` | When it began, if known, with a timezone label if it is not CT |
| `etr` | Estimated restoration time, if the source gives one |
| `source` | A URL to the report or tracker used for that row |
| `updated` | When that row's source was checked or last updated, in CT when practical |

## Preview locally

The page loads `data.json` with `fetch`, so it needs to be served over HTTP (opening the HTML file directly will not load the data).

```bash
cd power-outage-map
python3 -m http.server 8080
```

Then open <http://localhost:8080/>.

## GitHub Pages

Publish the contents of this folder as the site root (or copy them into `/docs` and choose that folder). After the first deploy, the site will be at `https://<user>.github.io/<repo>/` if the repository name is not a user/organization site.

Leaflet is loaded from the unpkg CDN. Map tiles come from OpenStreetMap and must keep the attribution shown in the footer and on the map.

## Sources used for the first snapshot

Figures in the initial `data.json` were checked on the afternoon of November 12, 2026 (CT) against PowerOutage.us, Unión Eléctrica / Cubadebate, Energex, ABC News, SABC News / City Power, WAPA reporting via VIsland Guide, ISMO coverage in The Thursday Times, Ukraine and Venezuela news reports, and Nigerian official/news updates. See the page footer for links.
