#!/usr/bin/env python3
"""Daily TrustMRR snapshot: fetch tracked segments via the authenticated API,
append history, and regenerate the JSON files the dashboard reads.

Outputs (all under data/):
  snapshots.jsonl   append-only history, one line per startup per day (idempotent per day)
  latest.json       today's full snapshot grouped by section
  series-90d.json   rolling 90-day MRR/rank series per startup (for sparklines)
  delta.json        notable changes vs the previous snapshot date

Env:
  TRUSTMRR_API_KEY  required, Bearer token
  FETCH_MAX_PAGES   optional cap on pages per section (for testing)

Note: despite the API docs claiming USD cents, observed values are USD dollars
(verified against the public leaderboard). All amounts here are dollars.
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

API = "https://trustmrr.com/api/v1/startups"
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

# section -> (query params, pages of 10)
SECTIONS = {
    "leaderboard": ({"sort": "revenue-desc"}, 10),
    "growth": ({"sort": "growth-desc", "minRevenue": "1000"}, 3),
    "deals": ({"onSale": "true", "sort": "best-deal"}, 10),
    "highMrrDeals": ({"onSale": "true", "minMrr": "2000", "sort": "revenue-desc"}, 5),
}

MILESTONES = [10_000, 50_000, 100_000, 500_000, 1_000_000]
REQUEST_GAP_S = 6.5  # 10 req/min limit


def get(params, key):
    qs = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{API}?{qs}", headers={"Authorization": f"Bearer {key}"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = 65
                print(f"rate limited, sleeping {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            if e.code >= 500 and attempt < 3:
                time.sleep(10 * (attempt + 1))
                continue
            raise
    raise RuntimeError(f"giving up on {qs}")


def fetch_section(name, params, pages, key):
    max_pages = int(os.environ.get("FETCH_MAX_PAGES", pages))
    items = []
    for page in range(1, min(pages, max_pages) + 1):
        resp = get({**params, "page": page, "limit": 10}, key)
        batch = resp.get("data", [])
        items.extend(batch)
        print(f"{name}: page {page} -> {len(batch)} items", file=sys.stderr)
        if not resp.get("meta", {}).get("hasMore"):
            break
        time.sleep(REQUEST_GAP_S)
    return items


def compact(item):
    rev = item.get("revenue") or {}
    return {
        "slug": item["slug"],
        "name": item["name"],
        "category": item.get("category"),
        "mrr": rev.get("mrr"),
        "rev30": rev.get("last30Days"),
        "total": rev.get("total"),
        "rank": item.get("rank"),
        "growth30d": item.get("growth30d"),
        "growthMRR30d": item.get("growthMRR30d"),
        "onSale": item.get("onSale", False),
        "price": item.get("askingPrice"),
        "multiple": item.get("multiple"),
    }


def load_history():
    path = DATA / "snapshots.jsonl"
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def pct_change(old, new):
    if not old:
        return None
    return (new - old) / abs(old) * 100


def build_delta(today, merged, prev_by_slug, prev_date):
    delta = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "date": today,
        "prevDate": prev_date,
        "newListings": [],
        "delisted": [],
        "priceChanges": [],
        "rankMoves": [],
        "mrrMoves": [],
        "milestones": [],
    }
    if prev_date is None:
        return delta

    prev_onsale = {s for s, r in prev_by_slug.items() if r.get("onSale")}
    for slug, rec in merged.items():
        prev = prev_by_slug.get(slug)
        if rec.get("onSale") and slug not in prev_onsale:
            delta["newListings"].append({
                "slug": slug, "name": rec["name"], "mrr": rec.get("mrr"),
                "price": rec.get("price"), "multiple": rec.get("multiple"),
                "category": rec.get("category"),
            })
        if prev is None:
            continue
        if prev.get("onSale") and not rec.get("onSale"):
            delta["delisted"].append({"slug": slug, "name": rec["name"], "lastPrice": prev.get("price")})
        old_p, new_p = prev.get("price"), rec.get("price")
        if old_p and new_p and old_p != new_p and abs(pct_change(old_p, new_p)) >= 5:
            delta["priceChanges"].append({
                "slug": slug, "name": rec["name"], "from": old_p, "to": new_p,
                "pct": round(pct_change(old_p, new_p), 1),
            })
        old_r, new_r = prev.get("rank"), rec.get("rank")
        if old_r and new_r and new_r <= 100 and abs(old_r - new_r) >= 10:
            delta["rankMoves"].append({"slug": slug, "name": rec["name"], "from": old_r, "to": new_r})
        old_m, new_m = prev.get("mrr"), rec.get("mrr")
        if old_m and new_m is not None:
            chg = pct_change(old_m, new_m)
            if chg is not None and abs(chg) >= 10 and abs(new_m - old_m) >= 500:
                delta["mrrMoves"].append({
                    "slug": slug, "name": rec["name"], "from": round(old_m), "to": round(new_m),
                    "pct": round(chg, 1),
                })
            for mark in MILESTONES:
                if old_m < mark <= new_m:
                    delta["milestones"].append({"slug": slug, "name": rec["name"], "crossed": mark, "direction": "up"})
                elif new_m < mark <= old_m:
                    delta["milestones"].append({"slug": slug, "name": rec["name"], "crossed": mark, "direction": "down"})

    # keep the payload small and highest-signal first
    delta["newListings"].sort(key=lambda x: x.get("mrr") or 0, reverse=True)
    delta["mrrMoves"].sort(key=lambda x: abs(x["pct"]), reverse=True)
    for k in ("newListings", "delisted", "priceChanges", "rankMoves", "mrrMoves", "milestones"):
        delta[k] = delta[k][:20]
    return delta


def main():
    key = os.environ.get("TRUSTMRR_API_KEY")
    if not key:
        sys.exit("TRUSTMRR_API_KEY is not set")
    DATA.mkdir(exist_ok=True)
    today = date.today().isoformat()

    sections, merged = {}, {}
    for name, (params, pages) in SECTIONS.items():
        items = fetch_section(name, params, pages, key)
        sections[name] = items
        for it in items:
            merged[it["slug"]] = compact(it)
        time.sleep(REQUEST_GAP_S)

    if not merged:
        sys.exit("no data fetched, aborting without writing")

    history = load_history()
    prev_dates = sorted({r["date"] for r in history if r["date"] != today})
    prev_date = prev_dates[-1] if prev_dates else None
    prev_by_slug = {r["slug"]: r for r in history if r["date"] == prev_date} if prev_date else {}

    # idempotent per-day append: drop any existing lines for today, then add fresh
    history = [r for r in history if r["date"] != today]
    today_rows = [{"date": today, **rec} for rec in merged.values()]
    history.extend(today_rows)
    with open(DATA / "snapshots.jsonl", "w", encoding="utf-8") as f:
        for r in history:
            f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")

    cutoff = sorted({r["date"] for r in history})[-90:]
    series = {}
    for r in history:
        if r["date"] not in cutoff:
            continue
        s = series.setdefault(r["slug"], {"name": r["name"], "points": []})
        s["points"].append([r["date"], r.get("mrr"), r.get("rank")])
    for s in series.values():
        s["points"].sort()
    with open(DATA / "series-90d.json", "w", encoding="utf-8") as f:
        json.dump(series, f, ensure_ascii=False, separators=(",", ":"))

    with open(DATA / "latest.json", "w", encoding="utf-8") as f:
        json.dump({
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "date": today,
            "sections": sections,
        }, f, ensure_ascii=False, separators=(",", ":"))

    delta = build_delta(today, merged, prev_by_slug, prev_date)
    with open(DATA / "delta.json", "w", encoding="utf-8") as f:
        json.dump(delta, f, ensure_ascii=False, separators=(",", ":"))

    print(f"done: {len(merged)} startups across {len(sections)} sections; prev={prev_date}", file=sys.stderr)


if __name__ == "__main__":
    main()
