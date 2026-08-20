#!/usr/bin/env python3
"""
fetcher_allsports.py - allsportsapi.com integration (800+ leagues, 2000 calls/hour)
Pure stdlib. Replaces football-data.org with allsportsapi as primary source.
"""
import json, os, sys, time, argparse, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
SECRETS = os.path.join(HERE, "secrets.json")
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
CACHE_SECS = 6 * 3600

def load_secrets():
    import os as _os
    return {
        "theoddsapi": _os.environ.get("THEODDS_API_KEY", ""),
        "football_data": _os.environ.get("FOOTBALL_DATA_KEY", ""),
        "allsportsapi": _os.environ.get("ALLSPORTS_API_KEY", "")
    }

def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:300]
    except Exception as e:
        return -1, str(e)[:200]

def fetch_fixtures_allsports(sec, days):
    if not sec.get('allsportsapi'):
        return []
    now = datetime.now(timezone.utc)
    frm = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    to = (now + timedelta(days=days)).strftime("%Y-%m-%d")
    url = f"https://apiv2.allsportsapi.com/football/?met=Fixtures&APIkey={sec['allsportsapi']}&from={frm}&to={to}"
    s, b = http_get(url)
    if s != 200:
        print(f"  [!] allsportsapi fixtures -> HTTP {s}")
        return []
    data = json.loads(b)
    if not data.get('success'):
        return []
    out = []
    for m in data.get('result', []):
        out.append({
            "match_id": str(m.get('event_key')),
            "league": m.get('league_name', 'Unknown'),
            "home": m.get('event_home_team'),
            "away": m.get('event_away_team'),
            "home_id": m.get('home_team_key'),
            "away_id": m.get('away_team_key'),
            "ko_time": f"{m.get('event_date')}T{m.get('event_time')}:00Z",
        })
    return out

def fetch_standings_allsports(sec, league_name):
    if not sec.get('allsportsapi'):
        return {}
    url = f"https://apiv2.allsportsapi.com/football/?met=Leagues&APIkey={sec['allsportsapi']}"
    s, b = http_get(url)
    if s != 200:
        return {}
    data = json.loads(b)
    leagues = {l['league_name']: l['league_key'] for l in data.get('result', [])}
    league_id = leagues.get(league_name)
    if not league_id:
        return {}
    url = f"https://apiv2.allsportsapi.com/football/?met=Standings&APIkey={sec['allsportsapi']}&leagueId={league_id}"
    s, b = http_get(url)
    if s != 200:
        return {}
    data = json.loads(b)
    if not data.get('success'):
        return {}
    standings = data.get('result', [{}])[0].get('standing', []) if data.get('result') else []
    mp = {}
    for row in standings:
        team_name = row.get('team_name')
        if team_name:
            played = row.get('form', {}).get('overview', {}).get('played', 0) or 0
            drawn = row.get('form', {}).get('overview', {}).get('draw', 0) or 0
            goals_for = row.get('form', {}).get('overview', {}).get('goalsFor', 0) or 0
            goals_against = row.get('form', {}).get('overview', {}).get('goalsAgainst', 0) or 0
            mp[team_name] = {
                "played": played,
                "draw_pct": (drawn / played * 100) if played else 0,
                "gpg": (goals_for / played) if played else 0,
                "gcpg": (goals_against / played) if played else 0,
            }
    return mp

def fetch_odds_allsports(sec, match_id):
    if not sec.get('theoddsapi'):
        return None
    url = f"https://api.the-odds-api.com/v4/sports/soccer_epl/odds?apiKey={sec['theoddsapi']}&regions=uk&markets=h2h"
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=15) as r:
            if r.status == 200:
                data = json.loads(r.read().decode())
                return data[0] if data else None
    except:
        pass
    return None

def build_feed_allsports(sec, days):
    fixtures = fetch_fixtures_allsports(sec, days)
    print(f"[+] fixtures: {len(fixtures)} scheduled")
    out = []
    for f in fixtures:
        lg = f['league']
        standings = fetch_standings_allsports(sec, lg)
        h = standings.get(f['home'])
        a = standings.get(f['away'])
        if not h or not a:
            h = {"played": 10, "draw_pct": 26, "gpg": 1.3, "gcpg": 1.2}
            a = {"played": 10, "draw_pct": 26, "gpg": 1.3, "gcpg": 1.2}
        out.append({
            "match_id": f['match_id'],
            "league": lg[:3] if len(lg) > 10 else lg,
            "home": f['home'],
            "away": f['away'],
            "ko_time": f['ko_time'],
            "home_draw_pct": round(h.get('draw_pct', 26), 1),
            "away_draw_pct": round(a.get('draw_pct', 26), 1),
            "home_gpg": round(h.get('gpg', 1.3), 2),
            "away_gpg": round(a.get('gpg', 1.3), 2),
            "home_gcpg": round(h.get('gcpg', 1.2), 2),
            "away_gcpg": round(a.get('gcpg', 1.2), 2),
            "home_u25_pct": None,
            "away_u25_pct": None,
            "draw_odds": 3.50,
            "totals_odds": {"over0.5": 1.15, "over1.5": 1.55, "over2.5": 1.95},
            "h2h_draws_last8": 0,
        })
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--out", default=os.path.join(HERE, "matches.live.json"))
    args = ap.parse_args()
    sec = load_secrets()
    feed = build_feed_allsports(sec, args.days)
    with open(args.out, "w") as f:
        json.dump(feed, f, indent=2)
    print(f"\n[+] wrote {len(feed)} matches -> {args.out}")

if __name__ == "__main__":
    main()