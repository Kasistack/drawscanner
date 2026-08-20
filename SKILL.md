---
description: Deploy Draw Scanner football betting bot to GitHub Actions
name: draw-scanner-deployment
---

# Draw Scanner Deployment

## Prerequisites
- GitHub CLI (`gh`) authenticated
- API keys: `THEODDS_API_KEY`, `FOOTBALL_DATA_KEY`  
- ntfy credentials: `NTFY_TOPIC`, `NTFY_TOPIC_TOTALS`, `NTFY_SERVER`

## Deployment Steps

### 1. Push to GitHub
```bash
cd ~/drawscanner
git add . && git commit -m "deploy: $(date +%Y%m%d)"
gh repo create drawscanner --public --push
```

### 2. Set GitHub Secrets
In repo: Settings → Secrets and variables → Actions

| Secret | Value |
|--------|-------|
| THEODDS_API_KEY | [from the-odds-api.com] |
| FOOTBALL_DATA_KEY | [from football-data.org] |
| NTFY_TOPIC | footballdraws |
| NTFY_TOPIC_TOTALS | footballtotals |
| NTFY_SERVER | https://ntfy.sh |

### 3. Verify Workflow
`.github/workflows/scan.yml`:
- Daily 06:00 UTC: fetch + scan (no form)
- Weekly Sun 06:30 UTC: with `--form` flag

## Key Files
| File | Purpose |
|------|---------|
| fetcher.py | Pulls fixtures/standings/odds |
| drawscanner.py | Filters + scores draw picks |
| totalscanner.py | Over/Under +EV detection |
| config.json | Thresholds |
| secrets.json | Local keys (gitignored) |

## Troubleshooting
- **Network issues**: Check Termux TLS to api.football-data.org
- **ntfy fails**: Verify GH Actions secrets
- **No picks**: Adjust thresholds in config.json