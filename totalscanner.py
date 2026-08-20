#!/usr/bin/env python3
"""
totalscanner.py - Over/Under (totals) betting scanner.
Pure stdlib. Reads the feed built by fetcher.py (matches.live.json) and ranks
matches by expected-value edge on the 0.5 / 1.5 / 2.5 lines.

Model: combined avg goals (gpg+gcpg both sides /4) drives a Poisson estimate of
the probability of going OVER each line. That model % is compared to the book's
implied probability (from the-odds-api totals price) -> edge = model% - book%.
Highest +EV first. Shots-on-target (SoT) volume is shown as a supporting signal
when present (from --form fetch); otherwise league average is noted.

Usage:
  python3 totalscanner.py --feed matches.live.json [--top 8] [--no-alert]

Alerts go to config.alerts.totals (ntfy topic footballtotals).
"""
import json, os, argparse, urllib.request, urllib.error
from datetime import datetime, timezone
from math import exp

import drawscanner as ds  # reuse config load + ntfy sender

REQ = ["match_id", "league", "home", "away", "ko_time",
       "home_gpg", "away_gpg", "home_gcpg", "away_gcpg"]

def poisson_over(lam, line):
    """P(total goals > line) via Poisson survival, line in {0.5,1.5,2.5}."""
    # sum_{k=0}^{floor(line)} e^-lam * lam^k / k!
    kmax = int(line)
    p_at_most = 0.0
    for k in range(kmax + 1):
        p_at_most += (lam ** k) / factorial(k)
    p_at_most *= exp(-lam)
    return 1.0 - p_at_most

def factorial(n):
    f = 1
    for i in range(2, n + 1):
        f *= i
    return f

def implied_prob(price):
    """decimal price -> implied probability (ignoring vig)."""
    try:
        return 1.0 / float(price)
    except (TypeError, ValueError, ZeroDivisionError):
        return None

def evaluate(m, cfg):
    lam = (m["home_gpg"] + m["away_gpg"] + m["home_gcpg"] + m["away_gcpg"]) / 4.0
    totals = m.get("totals_odds") or {}
    sot_league = cfg.get("totals", {}).get("sot_league_avg", {})
    sot = None
    hs, as_ = m.get("home_sot"), m.get("away_sot")
    if hs is not None and as_ is not None:
        sot = hs + as_
    lines = cfg.get("totals", {}).get("lines", [0.5, 1.5, 2.5])
    results = []
    for line in lines:
        model_p = poisson_over(lam, line)
        # book price: look for over{line} key
        key = f"over{line}"
        price = totals.get(key)
        book_p = implied_prob(price)
        edge = (model_p - book_p) if book_p else None
        results.append({
            "line": line,
            "model_p": model_p,
            "book_p": book_p,
            "price": price,
            "edge": edge,
        })
    # supporting SoT note
    sot_note = ""
    if sot is not None:
        sot_note = f"SoT {sot:.1f}"
    elif m["league"] in sot_league:
        sot_note = f"SoT ~{sot_league[m['league']]:.1f} (league avg)"
    return {"lam": lam, "results": results, "sot_note": sot_note}

def main():
    ap = argparse.ArgumentParser(description="Over/Under totals scanner")
    ap.add_argument("--feed", required=True)
    ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--no-alert", action="store_true")
    args = ap.parse_args()

    cfg = ds.load_config(None)
    # env overrides for ntfy (CI)
    import os as _os
    _t = cfg.setdefault("alerts", {}).setdefault("totals", {})
    if _os.environ.get("NTFY_TOPIC"):
        _t["enabled"] = True
        _t["topic"] = _os.environ["NTFY_TOPIC"]
    if _os.environ.get("NTFY_SERVER"):
        _t["server"] = _os.environ["NTFY_SERVER"]

    feed = ds.load_feed(args.feed)
    if not feed:
        print("[!] no matches"); return

    rows = []
    for m in feed:
        try:
            ev = evaluate(m, cfg)
        except Exception as e:
            continue
        any_odds = any(r["edge"] is not None for r in ev["results"])
        for r in ev["results"]:
            if r["edge"] is not None:
                rows.append({
                    "match": m, "line": r["line"], "lam": ev["lam"],
                    "model_p": r["model_p"], "book_p": r["book_p"],
                    "price": r["price"], "edge": r["edge"], "sot": ev["sot_note"],
                    "has_odds": True,
                })
            elif not any_odds:
                # stat-only fallback: rank by model over-probability
                rows.append({
                    "match": m, "line": r["line"], "lam": ev["lam"],
                    "model_p": r["model_p"], "book_p": None,
                    "price": None, "edge": None, "sot": ev["sot_note"],
                    "has_odds": False,
                })

    min_edge = cfg.get("totals", {}).get("min_edge", 0.0)
    # edge rows must clear min_edge; stat-only rows always shown
    rows = [r for r in rows if (r["has_odds"] and r["edge"] >= min_edge) or (not r["has_odds"])]
    # sort: edge rows first (by edge desc), then stat-only (by model over-prob desc)
    rows.sort(key=lambda r: (r["has_odds"], r["edge"] if r["edge"] is not None else 0,
                             r["model_p"]), reverse=True)

    print(f"Scanned {len(feed)} matches | {sum(1 for r in rows if r['has_odds'])} +EV lines, "
          f"{sum(1 for r in rows if not r['has_odds'])} stat-only (no odds)\n")
    if not rows:
        print("No totals candidates.")
    else:
        print(f"{'#':<3}{'LINE':<6}{'LEAGUE':<7}{'MATCH':<30}{'EST':<6}{'MODEL%':<8}{'BOOK%':<8}{'EDGE':<7}PRICE  SoT")
        for i, r in enumerate(rows[:args.top], 1):
            m = r["match"]
            tag = f"{m['home']} v {m['away']}"
            book_s = f"{r['book_p']*100:.1f}" if r["book_p"] else "n/a"
            edge_s = f"{r['edge']*100:+.1f}" if r["edge"] is not None else "n/a"
            price_s = str(r["price"]) if r["price"] else "n/a"
            model_s = f"{r['model_p']*100:.1f}" if r["model_p"] else "n/a"
            print(f"{i:<3}{r['line']:<6}{m['league'][:5]:<7}{tag[:28]:<30}{r['lam']:.2f}  "
                  f"{model_s:<8}{book_s:<8}{edge_s:<7}{price_s}  {r['sot']}")

    # payload
    picks = [{
        "rank": i + 1, "line": r["line"], "league": r["match"]["league"],
        "home": r["match"]["home"], "away": r["match"]["away"],
        "ko_time": r["match"]["ko_time"], "est_total": round(r["lam"], 2) if r["lam"] is not None else None,
        "model_pct": round(r["model_p"] * 100, 1) if r["model_p"] is not None else None,
        "book_pct": round(r["book_p"] * 100, 1) if r["book_p"] is not None else None,
        "edge_pct": round(r["edge"] * 100, 1) if r["edge"] is not None else None,
        "price": r["price"], "sot": r["sot"], "has_odds": r["has_odds"],
    } for i, r in enumerate(rows)]

    out_path = os.path.join(os.path.dirname(os.path.abspath(args.feed)), "totals_picks.json")
    with open(out_path, "w") as f:
        json.dump({"generated": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                   "count": len(picks), "picks": picks}, f, indent=2)
    print(f"\n[+] totals picks written to {out_path}")

    if args.no_alert:
        print("[*] --no-alert; dry-run payload below")
    if picks:
        body = build_totals_body(picks[:args.top])
        print("\n----- TOTALS NTFY PAYLOAD -----")
        print(body)
        print("----- end -----")
        if not args.no_alert:
            st = send_totals(body, cfg)
            if st:
                print(f"[+] totals ntfy sent (HTTP {st})")

def build_totals_body(picks):
    lines = ["# Totals Scanner - O/U candidates", ""]
    for p in picks:
        edge = f"{p['edge_pct']:+}%" if p.get("edge_pct") is not None else "n/a (stat-only)"
        price = p.get("price") or "n/a"
        book = f"{p['book_pct']}%" if p.get("book_pct") is not None else "n/a"
        lines.append(f"**{p['rank']}. {p['home']} v {p['away']}** ({p['league']}) O{p['line']}")
        lines.append(f"   est {p['est_total']} | model {p['model_pct']}% vs book {book} "
                     f"| edge {edge} | odds {price} | {p['sot']}")
        lines.append("")
    return "\n".join(lines)

def send_totals(body, cfg):
    a = cfg.get("alerts", {}).get("totals", {})
    if not a.get("enabled"):
        return None
    topic = a.get("topic", "footballtotals")
    server = a.get("server", "https://ntfy.sh").rstrip("/")
    url = f"{server}/{topic}"
    headers = {"Title": "Totals Scanner +EV", "Priority": "default", "Tags": "soccer,chart"}
    auth = a.get("auth_header")
    if auth:
        headers["Authorization"] = auth
    req = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status
    except urllib.error.URLError as e:
        print(f"[!] totals ntfy failed: {e}")
        return None

if __name__ == "__main__":
    main()
