# Draw Scanner — live draw-betting scanner

Pure-stdlib (no pip) football draw finder. Pulls live fixtures + standings from
football-data.org and live draw odds from the-odds-api.com, derives the stats,
runs the draw-betting checklist, and pushes the top picks to an ntfy topic.

## Files
- `fetcher.py`      — pulls live data, derives stats, joins odds, writes `matches.live.json`
- `drawscanner.py`  — filters `matches.live.json` against the checklist, scores, alerts
- `config.json`     — all thresholds, alert settings (edit here, no code changes)
- `matches.sample.json/.csv` — offline test feed (no network needed)

## Run locally
    python3 fetcher.py --days 7
    python3 drawscanner.py --feed matches.live.json

Options:
    fetcher.py  --days N  --no-odds  --form  (--form pulls recent U2.5/BTTS, slow)
    drawscanner.py  --feed FILE  --top N  --no-alert

## How the checklist works
Core gate (all must pass):
  - both teams draw% >= 30  (relaxed to 25 for preseason/historical-fallback teams, tagged [PRESEASON EST])
  - combined avg goals <= 2.3
  - draw odds >= 3.10
U2.5% gate is skipped when no recent-form data exists (early season).

Scoring: +1 H2H draws >=2 · +1 per situational flag · (league tiers removed — all leagues judged on stats).
Strict-signal picks (real standings) always rank above preseason estimates.

## CI / scheduled runs
`.github/workflows/scan.yml` runs on GitHub Actions:
  - daily 06:00 UTC: fetch + scan, push to ntfy (no form)
  - weekly Sun 06:30 UTC: same + `--form` (recent U2.5/BTTS, costs more API calls)
Set repo Secrets: `THEODDS_API_KEY`, `FOOTBALL_DATA_KEY`, `NTFY_TOPIC` (optional: `NTFY_SERVER`).

## Secrets
`secrets.json` is gitignored and used only for local dev. Never commit it.
