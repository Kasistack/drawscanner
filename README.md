# Draw Scanner (allsportsapi) — live draw-betting scanner

**Uses allsportsapi.com as primary data source** (800+ leagues, 2000 calls/hour)

## Files
- `fetcher_allsports.py` — pulls fixtures from allsportsapi + odds from the-odds-api
- `drawscanner.py` — filters matches against the checklist
- `totalscanner.py` — over/under +EV detection
- `config.json` — thresholds
- `secrets.json` — API keys

## Dev
```bash
python3 fetcher_allsports.py --days 7
python3 drawscanner.py --feed matches.live.json
python3 totalscanner.py --feed matches.live.json
```

## CI Secrets Required
- `THEODDS_API_KEY`
- `FOOTBALL_DATA_KEY` (optional, fallback)
- `ALLSPORTS_API_KEY`
- `NTFY_TOPIC`
- `NTFY_TOPIC_TOTALS`
- `NTFY_SERVER`