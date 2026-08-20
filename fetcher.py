#!/usr/bin/env python3
"""
fetcher.py - pulls live football data and builds feeds for the scanners.
Pure stdlib. Sources:
  - football-data.org  (fixtures, standings, recent form incl. shots-on-target)
  - the-odds-api.com   (draw odds + totals odds; browser UA required)
Outputs matches.live.json (schema consumed by drawscanner.py / totalscanner.py).

Usage:
  python3 fetcher.py [--days 7] [--out matches.live.json] [--no-odds] [--form]

Rate safety:
  FD  = 10 calls/min  -> 7s sleep between FD calls, bounded per run
  ODD = budgeted (default 45s wall-clock); on timeout/failure the scan still runs
        stat-only. Results cached 6h in odds.cache.json / odds.cache.totals.json
"""
import json, os, sys, time, argparse, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
SECRETS = os.path.join(HERE, "secrets.json")
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
FD_SLEEP = 7.0          # respect 10 calls/min
ODDS_CACHE_SECS = 6 * 3600

def load_secrets():
    """Prefer env vars (CI), else local secrets.json (chmod 600)."""
    import os as _os
    env_odds = _os.environ.get("THEODDS_API_KEY")
    env_fd = _os.environ.get("FOOTBALL_DATA_KEY")
    if env_odds and env_fd:
        return {"theoddsapi": env_odds, "football_data": env_fd,
                "allsportsapi": _os.environ.get("ALLSPORTS_API_KEY", "")}
    with open(SECRETS) as f:
        return json.load(f)

def http_get(url, headers=None, timeout=25, token=None):
    h = dict(UA); h.update(headers or {})
    if token:
        h["X-Auth-Token"] = token
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:300]
    except Exception as e:
        return -1, str(e)[:200]

def fd_get(path, sec, sleep=True, retries=3):
    last = None
    for attempt in range(retries):
        s, b = http_get("https://api.football-data.org/v4/" + path,
                        headers={"X-Auth-Token": sec["football_data"]})
        if s == 200:
            if sleep:
                time.sleep(FD_SLEEP)
            return json.loads(b)
        last = f"HTTP {s}: {b[:120]}"
        print(f"  [!] FD {path} attempt {attempt+1} -> {last}")
        time.sleep(3 * (attempt + 1))
    return None

# ---------- football-data.org ----------

def fetch_fixtures(sec, days):
    now = datetime.now(timezone.utc)
    frm = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    to = (now + timedelta(days=days)).strftime("%Y-%m-%d")
    d = fd_get(f"matches?status=SCHEDULED&dateFrom={frm}&dateTo={to}", sec)
    if not d:
        return []
    out = []
    for m in d.get("matches", []):
        out.append({
            "match_id": str(m["id"]),
            "league": m["competition"]["code"],
            "home": m["homeTeam"]["name"],
            "away": m["awayTeam"]["name"],
            "home_id": m["homeTeam"]["id"],
            "away_id": m["awayTeam"]["id"],
            "ko_time": m["utcDate"],
        })
    return out

def fetch_standings_map(sec, leagues):
    """league code -> {team_id: {played, draw_pct, gpg, gcpg}}"""
    res = {}
    for lg in leagues:
        d = fd_get(f"competitions/{lg}/standings", sec)
        if not d:
            continue
        table = []
        for st in d.get("standings", []):
            if st.get("type") == "TOTAL":
                table = st.get("table", [])
                break
        if not table:
            table = d.get("standings", [{}])[0].get("table", [])
        mp = {}
        for row in table:
            pg = row.get("playedGames") or 0
            mp[row["team"]["id"]] = {
                "played": pg,
                "draw_pct": (row.get("draw", 0) / pg * 100.0) if pg else 0.0,
                "gpg": (row.get("goalsFor", 0) / pg) if pg else 0.0,
                "gcpg": (row.get("goalsAgainst", 0) / pg) if pg else 0.0,
            }
        res[lg] = mp
    return res

def fetch_recent_form(sec, team_id, limit=10):
    """returns (u25_pct, btts_no_pct, sot_per_game) or (None,None,None) if insufficient"""
    d = fd_get(f"teams/{team_id}/matches?status=FINISHED&limit={limit}", sec)
    if not d:
        return None, None, None
    ms = d.get("matches", [])
    u25 = btts = tot = 0
    sot_sum = 0
    for m in ms:
        sc = m.get("score", {}).get("fullTime", {})
        h = sc.get("home"); a = sc.get("away")
        if h is None or a is None:
            continue
        tot += 1
        if h + a < 3:
            u25 += 1
        if not (h > 0 and a > 0):
            btts += 1  # btts NO
        if m.get("homeTeam", {}).get("id") == team_id:
            sot_sum += m.get("score", {}).get("homeTeam", {}).get("shotsOnTarget") or 0
        elif m.get("awayTeam", {}).get("id") == team_id:
            sot_sum += m.get("score", {}).get("awayTeam", {}).get("shotsOnTarget") or 0
    if tot < 5:
        return None, None, None
    return u25 / tot * 100.0, btts / tot * 100.0, sot_sum / tot

# ---------- the-odds-api.com ----------

def load_odds_cache():
    p = os.path.join(HERE, "odds.cache.json")
    if os.path.exists(p):
        try:
            with open(p) as f:
                c = json.load(f)
            if time.time() - c.get("ts", 0) < ODD_CACHE_SECS:
                odds = {}
                for k, v in c["odds"].items():
                    h, a = (k.split("||", 1) if "||" in k else (k, ""))
                    odds[(h, a)] = v
                return odds
        except Exception:
            pass
    return None

def save_odds_cache(odds):
    p = os.path.join(HERE, "odds.cache.json")
    with open(p, "w") as f:
        json.dump({"ts": time.time(), "odds": {f"{h}||{a}": v for (h, a), v in odds.items()}}, f)

def fetch_odds(sec, budget=45):
    cached = load_odds_cache()
    if cached is not None:
        print("[*] using cached odds (<=6h old)")
        return cached
    key = sec["theoddsapi"]
    sports = ["soccer_epl", "soccer_germany_bundesliga", "soccer_netherlands_eredivisie",
              "soccer_brazil_campeonato", "soccer_spain_la_liga", "soccer_france_ligue_one",
              "soccer_efl_champ", "soccer_portugal_primeira_liga",
              "soccer_italy_serie_a", "soccer_uefa_champs_league_qualification"]
    odds = {}
    started = time.time()
    for sp in sports:
        if time.time() - started > budget:
            print(f"  [!] odds budget {budget}s hit; using {len(odds)} collected")
            break
        url = (f"https://api.the-odds-api.com/v4/sports/{sp}/odds?apiKey={key}"
               f"&regions=uk&markets=h2h")
        s, b = http_get(url, timeout=12)
        if s != 200:
            print(f"  [!] odds {sp} -> HTTP {s}")
            continue
        try:
            for g in json.loads(b):
                home = g.get("home_team"); away = g.get("away_team")
                draw = None
                for bm in g.get("bookmakers", [])[:1]:
                    for mk in bm.get("markets", []):
                        if mk.get("key") == "h2h":
                            for o in mk.get("outcomes", []):
                                if o.get("name", "").lower() == "draw":
                                    draw = o.get("price")
                if draw is not None:
                    odds[(home, away)] = draw
        except Exception:
            pass
    if odds:
        save_odds_cache(odds)
        print(f"[*] odds fetched: {len(odds)} games with draw line")
    else:
        print("[!] odds fetch produced nothing (network); draw scan runs stat-only")
    return odds

def load_totals_cache():
    p = os.path.join(HERE, "odds.cache.totals.json")
    if os.path.exists(p):
        try:
            with open(p) as f:
                c = json.load(f)
            if time.time() - c.get("ts", 0) < ODD_CACHE_SECS:
                odds = {}
                for k, v in c["odds"].items():
                    h, a = (k.split("||", 1) if "||" in k else (k, ""))
                    odds[(h, a)] = v
                print("[*] using cached totals odds (<=6h old)")
                return odds
        except Exception:
            pass
    return None

def save_totals_cache(odds):
    p = os.path.join(HERE, "odds.cache.totals.json")
    with open(p, "w") as f:
        json.dump({"ts": time.time(), "odds": {f"{h}||{a}": v for (h, a), v in odds.items()}}, f)

def fetch_totals_odds(sec, budget=45):
    cached = load_totals_cache()
    if cached is not None:
        return cached
    key = sec["theoddsapi"]
    sports = ["soccer_epl", "soccer_germany_bundesliga", "soccer_netherlands_eredivisie",
              "soccer_brazil_campeonato", "soccer_spain_la_liga", "soccer_france_ligue_one",
              "soccer_efl_champ", "soccer_portugal_primeira_liga",
              "soccer_italy_serie_a", "soccer_uefa_champs_league_qualification"]
    odds = {}
    started = time.time()
    for sp in sports:
        if time.time() - started > budget:
            print(f"  [!] totals budget {budget}s hit; using {len(odds)} collected")
            break
        url = (f"https://api.the-odds-api.com/v4/sports/{sp}/odds?apiKey={key}"
               f"&regions=uk&markets=totals")
        s, b = http_get(url, timeout=12)
        if s != 200:
            print(f"  [!] totals {sp} -> HTTP {s}")
            continue
        try:
            for g in json.loads(b):
                home = g.get("home_team"); away = g.get("away_team")
                rec = {}
                for bm in g.get("bookmakers", [])[:1]:
                    for mk in bm.get("markets", []):
                        if mk.get("key") == "totals":
                            for o in mk.get("outcomes", []):
                                name = o.get("name", "")
                                pt = o.get("point")
                                if pt is None:
                                    try:
                                        pt = float(name.split()[-1])
                                    except Exception:
                                        continue
                                rec[f"{name.split()[0].lower()}{pt}"] = o.get("price")
                if rec:
                    odds[(home, away)] = rec
        except Exception:
            pass
    if odds:
        save_totals_cache(odds)
        print(f"[*] totals odds fetched: {len(odds)} games")
    else:
        print("[!] totals odds produced nothing (network); totals scan runs stat-only")
    return odds

# ---------- name normalization for odds join ----------

import re
_NOISE = {"fc", "afc", "cf", "united", "city", "town", "athletic", "real",
          "club", "de", "sd", "sc", "ac", "as", "rc", "fk", "cd"}

def _translit(s):
    s = s.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    s = s.replace("ae", "a").replace("oe", "o").replace("ue", "u")
    return s

def norm_name(n):
    n = _translit(n.lower())
    toks = [t for t in re.split(r"[^a-z0-9]+", n) if t and t not in _NOISE]
    return " ".join(toks)

def match_odds(home, away, odds):
    if (home, away) in odds:
        return odds[(home, away)]
    nh, na = norm_name(home), norm_name(away)
    if not nh or not na:
        return None
    for (h, a), v in odds.items():
        oh, oa = norm_name(h), norm_name(a)
        for (t1, t2) in ((oh, oa), (oa, oh)):
            if (nh in t1 or t1 in nh) and (na in t2 or t2 in na):
                return v
    return None

def match_totals(home, away, totals):
    if (home, away) in totals:
        return totals[(home, away)]
    nh, na = norm_name(home), norm_name(away)
    if not nh or not na:
        return None
    for (h, a), v in totals.items():
        oh, oa = norm_name(h), norm_name(a)
        for (t1, t2) in ((oh, oa), (oa, oh)):
            if (nh in t1 or t1 in nh) and (na in t2 or t2 in na):
                return v
    return None

# ---------- build feed ----------

LEAGUE_DRAW_AVG = {
    "PL": 26.0, "BL1": 25.0, "DED": 27.0, "BSA": 28.0, "PD": 27.0,
    "FL1": 27.0, "ELC": 28.0, "PPL": 27.0, "SA": 26.0, "CL": 24.0,
    "EC": 25.0, "SPL": 28.0,
}
LEAGUE_GOAL_AVG = {
    "PL": 1.35, "BL1": 1.55, "DED": 1.45, "BSA": 1.20, "PD": 1.30,
    "FL1": 1.25, "ELC": 1.30, "PPL": 1.25, "SA": 1.25, "CL": 1.40,
    "EC": 1.30, "SPL": 1.35,
}
MIN_GAMES_FOR_STAT = 5

def build_feed(sec, days, use_odds, use_form):
    fixtures = fetch_fixtures(sec, days)
    print(f"[+] fixtures: {len(fixtures)} scheduled")
    leagues = sorted({f["league"] for f in fixtures})
    standings = fetch_standings_map(sec, leagues)
    print(f"[+] standings pulled for {len(standings)} leagues")

    odds = fetch_odds(sec) if use_odds else {}
    totals = fetch_totals_odds(sec) if use_odds else {}

    out = []
    form_cache = {}
    for f in fixtures:
        lg = f["league"]
        sm = standings.get(lg, {})
        h = sm.get(f["home_id"]); a = sm.get(f["away_id"])
        if not h or not a:
            continue
        hist_draw = LEAGUE_DRAW_AVG.get(lg, 26.0)
        hist_goal = LEAGUE_GOAL_AVG.get(lg, 1.3)
        h_draw = hist_draw if h["played"] < MIN_GAMES_FOR_STAT else h["draw_pct"]
        a_draw = hist_draw if a["played"] < MIN_GAMES_FOR_STAT else a["draw_pct"]
        h_gpg = hist_goal if h["played"] < MIN_GAMES_FOR_STAT else h["gpg"]
        a_gpg = hist_goal if a["played"] < MIN_GAMES_FOR_STAT else a["gpg"]
        h_gcpg = hist_goal if h["played"] < MIN_GAMES_FOR_STAT else h["gcpg"]
        a_gcpg = hist_goal if a["played"] < MIN_GAMES_FOR_STAT else a["gcpg"]
        rec = {
            "match_id": f["match_id"], "league": lg, "home": f["home"],
            "away": f["away"], "ko_time": f["ko_time"],
            "home_draw_pct": round(h_draw, 1), "away_draw_pct": round(a_draw, 1),
            "home_gpg": round(h_gpg, 2), "away_gpg": round(a_gpg, 2),
            "home_gcpg": round(h_gcpg, 2), "away_gcpg": round(a_gcpg, 2),
            "h2h_draws_last8": 0,
        }
        if use_form:
            for tid, keyu, keyb, keys in [(f["home_id"], "home_u25_pct", "home_btts_no_pct", "home_sot"),
                                           (f["away_id"], "away_u25_pct", "away_btts_no_pct", "away_sot")]:
                if tid not in form_cache:
                    form_cache[tid] = fetch_recent_form(sec, tid)
                u25, bttsno, sot = form_cache[tid]
                if u25 is not None:
                    rec[keyu] = round(u25, 1); rec[keyb] = round(bttsno, 1)
                if sot is not None:
                    rec[keys] = round(sot, 1)
        if use_odds:
            rec["draw_odds"] = match_odds(f["home"], f["away"], odds)
            rec["totals_odds"] = match_totals(f["home"], f["away"], totals)
        else:
            rec["draw_odds"] = None
            rec["totals_odds"] = None
        out.append(rec)
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--out", default=os.path.join(HERE, "matches.live.json"))
    ap.add_argument("--no-odds", action="store_true")
    ap.add_argument("--form", action="store_true", help="pull recent-form U2.5/BTTS/SoT (slow)")
    args = ap.parse_args()
    sec = load_secrets()
    feed = build_feed(sec, args.days, not args.no_odds, args.form)
    with open(args.out, "w") as f:
        json.dump(feed, f, indent=2)
    print(f"\n[+] wrote {len(feed)} matches -> {args.out}")

if __name__ == "__main__":
    main()
