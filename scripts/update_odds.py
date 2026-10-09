#!/usr/bin/env python3
"""
Pull Democratic win probabilities for each 2026 Senate race from Kalshi and
write them into the ODDS_DATA block of 2026_Senate_Tracker.html.

Only the text between /* ODDS:BEGIN */ and /* ODDS:END */ is rewritten (plus
the KALSHI_SERIES block when --discover verifies a new ticker). Races, hand
ratings, overrides and layout code are never touched.

Usage:
  python scripts/update_odds.py                 # refresh odds
  python scripts/update_odds.py --discover      # also try to verify missing tickers
  python scripts/update_odds.py --dry-run       # print results, don't write

Pricing: for each market, the bid/ask midpoint when the spread is <= MAX_SPREAD,
otherwise the last traded price. When both a D-side and an R-side market are
priced, the D probability is normalised over all priced markets in the event
(removes the overround). Standard library only.
"""
import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
MAX_SPREAD = 0.10
HTML_PATH = Path(__file__).resolve().parent.parent / "2026_Senate_Tracker.html"

KALSHI_RE = re.compile(r"(const KALSHI_SERIES = )(\{.*?\})(;\s*/\* KALSHI:END \*/)", re.S)
ODDS_RE = re.compile(r"(const ODDS_DATA = )(\{.*?\})(;\s*/\* ODDS:END \*/)", re.S)
RACE_RE = re.compile(r"\{id:'([A-Z]{2})'.*?state:'([^']+)',cands:\[(.*?)\]\}")
CAND_RE = re.compile(r"""n:(['"])(.*?)\1,p:'([A-Z])'(?:,caucus:'([A-Z])')?""")


# ---------------------------------------------------------------- HTML I/O
def read_html(path):
    html = path.read_text(encoding="utf-8")
    m_k, m_o = KALSHI_RE.search(html), ODDS_RE.search(html)
    if not m_k or not m_o:
        sys.exit("Couldn't find the KALSHI_SERIES / ODDS_DATA blocks in the HTML.")
    races = []
    for rid, state, cands_src in RACE_RE.findall(html):
        cands = [{"n": n, "p": p, "caucus": c or None} for _, n, p, c in CAND_RE.findall(cands_src)]
        races.append({"id": rid, "state": state, "cands": cands})
    if not races:
        sys.exit("Couldn't parse the RACES array.")
    return html, json.loads(m_k.group(2)), races


def write_html(path, html, series_map, odds):
    odds_js = json.dumps(odds, indent=1, sort_keys=True)
    series_js = "{\n" + ",\n".join(
        f'  {json.dumps(k)}: {json.dumps(v)}' for k, v in series_map.items()) + "\n}"
    html = ODDS_RE.sub(lambda m: m.group(1) + odds_js + m.group(3), html, count=1)
    html = KALSHI_RE.sub(lambda m: m.group(1) + series_js + m.group(3), html, count=1)
    path.write_text(html, encoding="utf-8")


# ---------------------------------------------------------------- Kalshi
def get_json(path, params, retries=3):
    url = f"{BASE_URL}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "User-Agent": "senate-tracker-odds/1.0"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise
        except urllib.error.URLError:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise


def open_2026_event(series, state):
    """Return the open 2026 event in this series whose title names the state, else None."""
    data = get_json("/events", {"series_ticker": series, "status": "open",
                                "with_nested_markets": "true", "limit": 50})
    for ev in data.get("events", []):
        title = (ev.get("title") or "").lower()
        is_2026 = (ev.get("event_ticker", "").upper().endswith("-26")
                   or "2026" in (ev.get("title") or "") or "2026" in (ev.get("sub_title") or ""))
        if is_2026 and state.lower() in title:
            return ev
    return None


def _num(v):
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


def price_field(m, name):
    d = _num(m.get(name + "_dollars"))
    if d is not None:
        return d
    c = _num(m.get(name))
    return None if c is None else c / 100


def market_price(m):
    bid, ask, last = price_field(m, "yes_bid"), price_field(m, "yes_ask"), price_field(m, "last_price")
    if bid and ask and bid > 0 and 0 < ask < 1 and ask >= bid and ask - bid <= MAX_SPREAD:
        return (bid + ask) / 2, "mid"
    if last and last > 0:
        return last, "last"
    if bid and ask and bid > 0 and 0 < ask <= 1 and ask >= bid:
        return (bid + ask) / 2, "mid"
    return None


def surname(name):
    return re.sub(r"[^a-z\s'-]", "", name.lower()).split()[-1]


def side_of(m, race):
    t = str(m.get("ticker", "")).upper()
    if t.endswith("-D"):
        return "D"
    if t.endswith("-R"):
        return "R"
    sub = " ".join(filter(None, [m.get("yes_sub_title"), m.get("subtitle")])).lower()
    for label in (sub, str(m.get("title", "")).lower()):
        if not label:
            continue
        if "democrat" in label:
            return "D"
        if "republican" in label:
            return "R"
        for c in race["cands"]:
            sn = surname(c["n"])
            if len(sn) > 2 and re.search(r"\b" + re.escape(sn) + r"\b", label):
                return c["caucus"] or c["p"]
    return "?"


def d_prob(markets, race):
    d_sum = r_sum = total = 0.0
    have_d = have_r = False
    hows = set()
    for m in markets:
        if m.get("status") and m["status"] not in ("active", "open"):
            continue
        pr = market_price(m)
        if not pr:
            continue
        p, how = pr
        hows.add(how)
        total += p
        side = side_of(m, race)
        if side == "D":
            d_sum += p; have_d = True
        elif side == "R":
            r_sum += p; have_r = True
    if have_d and have_r and total > 0:
        d = d_sum / total
    elif have_d:
        d = d_sum
    elif have_r:
        d = 1 - r_sum
    else:
        return None
    return max(0.0, min(1.0, d)), "+".join(sorted(hows))


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--html", type=Path, default=HTML_PATH)
    ap.add_argument("--discover", action="store_true",
                    help="for states with no ticker, try SENATE<ST> and keep it only if the API "
                         "returns an open 2026 event whose title names that state")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    html, series_map, races = read_html(args.html)
    odds = {"updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": "snapshot", "races": {}}
    report = []

    for race in races:
        rid, state = race["id"], race["state"]
        series = series_map.get(rid)
        candidates = [series] if series else ([f"SENATE{rid}"] if args.discover else [])
        if not candidates:
            report.append((rid, "NO TICKER", "hand rating used"))
            continue
        for cand in candidates:
            try:
                ev = open_2026_event(cand, state)
            except urllib.error.HTTPError as e:
                msg = "no such series on Kalshi" if e.code == 404 else f"FETCH FAILED (HTTP {e.code})"
                report.append((rid, cand, f"{msg}; hand rating used"))
                break
            except Exception as e:  # network / timeout
                report.append((rid, cand, f"FETCH FAILED ({e}); hand rating used"))
                break
            if not ev:
                report.append((rid, cand, "no open 2026 event naming this state; hand rating used"))
                break
            res = d_prob(ev.get("markets", []), race)
            if not res:
                report.append((rid, ev["event_ticker"], "couldn't identify D/R markets; hand rating used"))
                break
            d, how = res
            odds["races"][rid] = {"d": round(d, 4), "series": cand,
                                  "event": ev["event_ticker"], "how": how}
            if not series:
                series_map[rid] = cand  # verified by title match above
                report.append((rid, ev["event_ticker"], f"DISCOVERED + verified ({ev.get('title')}) D={d:.0%}"))
            else:
                report.append((rid, ev["event_ticker"], f"D={d:.0%} via {how}"))
        time.sleep(0.15)  # stay well under Kalshi's public rate limit

    for rid, tick, msg in report:
        print(f"{rid:3} {tick:16} {msg}")
    print(f"\n{len(odds['races'])}/{len(races)} races priced from Kalshi.")

    if not odds["races"]:
        print("No odds fetched; leaving the HTML unchanged.")
        return 1
    if not args.dry_run:
        write_html(args.html, html, series_map, odds)
        print(f"Wrote {args.html.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
