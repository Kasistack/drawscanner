#!/usr/bin/env python3
"""
DRAW SCANNER / BOT  -  pure stdlib, no pip.
Filters a matches feed (JSON or CSV) against a draw-betting checklist,
scores/ranges the survivors, and optionally alerts via ntfy / webhook.

Usage:
  python drawscanner.py --feed matches.json [--config config.json] [--top 5] [--no-alert]
  python drawscanner.py --feed matches.csv  [--config config.json]

Config holds every threshold, league tier, and alert setting so you tune
without touching code. See config.json (default shipped alongside).
"""

import json
import csv
import sys
import os
import argparse
import urllib.request
import urllib.error
from datetime import datetime, timezone

REQUIRED = [
    "match_id", "league", "home", "away", "ko_time",
    "home_draw_pct", "away_draw_pct",
    "home_gpg", "away_gpg", "home_gcpg", "away_gcpg",
    "home_u25_pct", "away_u25_pct",
    "h2h_draws_last8", "draw_odds", "totals_odds",
]
OPTIONAL_FLAGS = [
    "must_win_home", "must_win_away", "derby", "euro_first_leg",
    "relegation_battle", "midtable_idle", "away_underdog_solid",
]


def to_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def load_config(path):
    if not path:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"[!] config not found at {path}; using built-in defaults")
        return {}


def load_feed(path):
    """Return list of normalized match dicts from JSON or CSV."""
    ext = os.path.splitext(path)[1].lower()
    rows = []
    if ext == ".csv":
        with open(path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for r in reader:
                rows.append(r)
    else:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        rows = data if isinstance(data, list) else data.get("matches", [])
    return [normalize(m) for m in rows]


def normalize(m):
    """Coerce types; fill optional flags."""
    out = {}
    for k in REQUIRED:
        out[k] = m.get(k)
    out["home_gpg"] = to_float(m.get("home_gpg"))
    out["away_gpg"] = to_float(m.get("away_gpg"))
    out["home_gcpg"] = to_float(m.get("home_gcpg"))
    out["away_gcpg"] = to_float(m.get("away_gcpg"))
    out["home_draw_pct"] = to_float(m.get("home_draw_pct"))
    out["away_draw_pct"] = to_float(m.get("away_draw_pct"))
    out["home_u25_pct"] = to_float(m.get("home_u25_pct")) if m.get("home_u25_pct") not in (None, "") else None
    out["away_u25_pct"] = to_float(m.get("away_u25_pct")) if m.get("away_u25_pct") not in (None, "") else None
    out["h2h_draws_last8"] = int(to_float(m.get("h2h_draws_last8")))
    out["draw_odds"] = to_float(m.get("draw_odds")) if m.get("draw_odds") not in (None, "") else None
    for flag in OPTIONAL_FLAGS:
        out[flag] = bool(m.get(flag, False))
    return out


def evaluate(m, cfg):
    t = cfg.get("thresholds", {})
    tiers = cfg.get("league_tiers", {})
    min_draw = t.get("min_draw_pct", 30.0)
    max_cag = t.get("max_combined_avg_goals", 2.3)
    min_u25 = t.get("min_u25_pct", 55.0)
    min_odds = t.get("min_draw_odds", 3.10)
    h2h_bonus_min = t.get("h2h_min_draws_bonus", 2)

    reasons = []
    fails = []

    # --- CORE GATE (draw%) ---
    both_draw = m["home_draw_pct"] >= min_draw and m["away_draw_pct"] >= min_draw
    if both_draw:
        reasons.append(f"both draw% >= {min_draw:.0f} ({m['home_draw_pct']:.0f}/{m['away_draw_pct']:.0f})")
    else:
        fails.append("draw% gate")

    cag = (m["home_gpg"] + m["away_gpg"] + m["home_gcpg"] + m["away_gcpg"]) / 4.0
    goals_ok = cag <= max_cag
    if goals_ok:
        reasons.append(f"combined avg goals {cag:.2f} <= {max_cag}")
    else:
        fails.append("goals gate")

    if m["home_u25_pct"] is None or m["away_u25_pct"] is None:
        # no recent-form data (e.g. early season) -> skip gate, don't fail
        reasons.append("U2.5% skipped (no recent-form data)")
    else:
        both_u25 = m["home_u25_pct"] >= min_u25 and m["away_u25_pct"] >= min_u25
        if both_u25:
            reasons.append(f"both U2.5% >= {min_u25:.0f} ({m['home_u25_pct']:.0f}/{m['away_u25_pct']:.0f})")
        else:
            fails.append("U2.5 gate")

    if m["draw_odds"] is None:
        fails.append("no draw odds")
    elif m["draw_odds"] >= min_odds:
        reasons.append(f"draw odds {m['draw_odds']:.2f} >= {min_odds}")
    else:
        fails.append("odds gate")

    if fails:
        return {"passed": False, "score": 0, "reasons": reasons, "fails": fails, "cag": cag}

    # --- SCORING (only reached if core gate passed) ---
    score = 4  # core hits
    if m["h2h_draws_last8"] >= h2h_bonus_min:
        score += 1
        reasons.append(f"H2H draws {m['h2h_draws_last8']} (>= {h2h_bonus_min})")

    s_boost = cfg.get("situational_boost", 1)
    for flag, label in [
        ("derby", "derby/cagey"),
        ("euro_first_leg", "euro first leg"),
        ("relegation_battle", "relegation battle"),
        ("midtable_idle", "midtable idle"),
        ("away_underdog_solid", "away underdog solid"),
    ]:
        if m.get(flag):
            score += s_boost
            reasons.append(label)

    return {"passed": True, "score": score, "reasons": reasons, "fails": [], "cag": cag}


def build_ntfy_body(picks, cfg):
    top = cfg.get("top_n", 5)
    picks = picks[:top]
    lines = ["# Draw Scanner - Top Picks", ""]
    for i, p in enumerate(picks, 1):
        m = p["match"]
        lines.append(f"**{i}. {m['home']} v {m['away']}** ({m['league']})")
        lines.append(f"   score {p['score']} | draw odds {m['draw_odds']:.2f} | KO {m['ko_time']}")
        lines.append(f"   {', '.join(p['reasons'][:4])}")
        lines.append("")
    return "\n".join(lines)


def send_ntfy(body, cfg):
    a = cfg.get("alerts", {}).get("ntfy", {})
    if not a.get("enabled"):
        return None
    topic = a.get("topic", "REPLACE_ME")
    server = a.get("server", "https://ntfy.sh").rstrip("/")
    url = f"{server}/{topic}"
    headers = {"Title": "Draw Scanner Picks", "Priority": "default", "Tags": "soccer,alarm"}
    auth = a.get("auth_header")
    if auth:
        headers["Authorization"] = auth
    req = urllib.request.Request(url, data=body.encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status
    except urllib.error.URLError as e:
        print(f"[!] ntfy send failed: {e}")
        return None


def send_webhook(payload, cfg):
    a = cfg.get("alerts", {}).get("webhook", {})
    if not a.get("enabled"):
        return None
    url = a.get("url", "REPLACE_ME")
    if url == "REPLACE_ME":
        return None
    headers = {"Content-Type": "application/json"}
    auth = a.get("auth_header")
    if auth:
        headers["Authorization"] = auth
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status
    except urllib.error.URLError as e:
        print(f"[!] webhook send failed: {e}")
        return None


def main():
    ap = argparse.ArgumentParser(description="Draw betting scanner")
    ap.add_argument("--feed", required=True, help="JSON or CSV matches feed")
    ap.add_argument("--config", default=None, help="config.json path")
    ap.add_argument("--top", type=int, default=None, help="how many picks to show/alert")
    ap.add_argument("--no-alert", action="store_true", help="skip sending alerts")
    args = ap.parse_args()

    cfg = load_config(args.config)
    # CI env overrides for ntfy
    import os as _os
    _ntfy = cfg.setdefault("alerts", {}).setdefault("ntfy", {})
    if _os.environ.get("NTFY_TOPIC"):
        _ntfy["enabled"] = True
        _ntfy["topic"] = _os.environ["NTFY_TOPIC"]
    if _os.environ.get("NTFY_SERVER"):
        _ntfy["server"] = _os.environ["NTFY_SERVER"]
    top_n = args.top or cfg.get("top_n", 5)

    matches = load_feed(args.feed)
    if not matches:
        print("[!] no matches loaded from feed")
        sys.exit(1)

    results = []
    for m in matches:
        r = evaluate(m, cfg)
        r["match"] = m
        results.append(r)

    passed = [r for r in results if r["passed"]]
    passed.sort(key=lambda r: (r["score"], r["match"]["draw_odds"]), reverse=True)

    print(f"Scanned {len(matches)} matches | {len(passed)} passed core gate\n")
    if not passed:
        print("No matches qualified.")
    else:
        print(f"{'#':<3}{'LEAGUE':<22}{'MATCH':<28}{'SC':<4}{'ODDS':<6}REASONS")
        for i, p in enumerate(passed[:top_n], 1):
            m = p["match"]
            tag = f"{m['home']} v {m['away']}"
            rs = "; ".join(p["reasons"][:3])
            print(f"{i:<3}{m['league'][:20]:<22}{tag[:26]:<28}{p['score']:<4}{m['draw_odds']:<6.2f}{rs}")

    # full detail dump
    out_path = os.path.join(os.path.dirname(os.path.abspath(args.feed)), "draw_picks.json")
    payload = {
        "generated": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "total_scanned": len(matches),
        "passed": len(passed),
        "picks": [
            {
                "rank": i + 1,
                "score": p["score"],
                "match_id": p["match"]["match_id"],
                "league": p["match"]["league"],
                "home": p["match"]["home"],
                "away": p["match"]["away"],
                "ko_time": p["match"]["ko_time"],
                "draw_odds": p["match"]["draw_odds"],
                "combined_avg_goals": round(p["cag"], 3),
                "reasons": p["reasons"],
            }
            for i, p in enumerate(passed)
        ],
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    print(f"\n[+] full picks written to {out_path}")

    # alerts
    if args.no_alert:
        print("[*] --no-alert set; skipping alerts (dry-run payload below)")
    if passed:
        body = build_ntfy_body(passed, cfg)
        print("\n----- NTFY PAYLOAD (dry-run) -----")
        print(body)
        print("----- end payload -----")
        if not args.no_alert:
            st = send_ntfy(body, cfg)
            if st:
                print(f"[+] ntfy sent (HTTP {st})")
            wb = send_webhook(payload, cfg)
            if wb:
                print(f"[+] webhook sent (HTTP {wb})")


if __name__ == "__main__":
    main()
