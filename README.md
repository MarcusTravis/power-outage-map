# ODIN shadow-mode output

This branch is written only by `.github/workflows/odin-shadow.yml` on `main`.
It is NOT part of the live site (GitHub Pages serves `main`).

- `shadow/odin_us.json`: latest US summary from DOE/ORNL ODIN (https://odin.ornl.gov/), in data.json row shape.
- `shadow/odin_log.csv`: append-only log, one line per run, with the poweroutage.us figures from `main`'s data.json alongside for comparison.
