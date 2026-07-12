#!/usr/bin/env python3
"""Daily TrustMRR snapshot, founder-lens edition.

Tracks small-but-promising products (not top-revenue incumbents), the user's
focus categories, and market heat (user demand vs capital pricing), then
regenerates the JSON files the dashboard reads.

Outputs (all under data/):
  snapshots.jsonl   append-only history, one line per startup per day (idempotent per day)
  latest.json       today's full snapshot grouped by section
  series-90d.json   rolling 90-day MRR/rank series per startup (for sparklines)
  delta.json        notable changes vs the previous snapshot date
  heat.json         current per-category market heat (user & capital lenses)
  heat.jsonl        daily heat history, one line per category per day
  census.json       weekly per-category census (total + on-sale counts)

Env:
  TRUSTMRR_API_KEY   required, Bearer token
  FETCH_MAX_PAGES    optional cap on pages per section (for testing)
  CENSUS_CATEGORIES  optional comma list to limit census (for testing)
  FORCE_CENSUS=1     run census regardless of age

Note: despite the API docs claiming USD cents, observed values are USD dollars
(verified against the public leaderboard). All amounts here are dollars.
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from statistics import median

API = "https://trustmrr.com/api/v1/startups"
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

# ---- founder-lens tracking segments -------------------------------------
# small-but-promising band: last-30d revenue $200..$15k, ranked by growth
SECTIONS_DAILY = {
    "potential": ({"minRevenue": "200", "maxRevenue": "15000", "sort": "growth-desc"}, 5),
    "recentListings": ({"onSale": "true", "sort": "listed-desc"}, 5),
    "smallDeals": ({"onSale": "true", "maxPrice": "50000", "sort": "best-deal"}, 3),
}
FOCUS_CATEGORIES = ["mobile-apps", "games", "health-fitness", "productivity", "utilities"]
FOCUS_PARAMS = {"minRevenue": "100", "maxRevenue": "20000", "sort": "growth-desc"}
FOCUS_PAGES = 2

ALL_CATEGORIES = [
    "ai", "saas", "developer-tools", "fintech", "marketing", "ecommerce",
    "productivity", "design-tools", "no-code", "analytics", "crypto-web3",
    "education", "health-fitness", "social-media", "content-creation", "sales",
    "customer-support", "recruiting", "real-estate", "travel", "legal",
    "security", "iot-hardware", "green-tech", "entertainment", "games",
    "community", "news-magazines", "utilities", "marketplace", "mobile-apps",
]

# founder-relevant milestones: what a small product crossing feels like
MILESTONES = [1_000, 5_000, 10_000, 25_000, 50_000, 100_000]
REQUEST_GAP_S = 6.5  # 10 req/min limit
CENSUS_MAX_AGE_DAYS = 6

FLAG_PATTERNS = {
    "ios": re.compile(r"\b(ios|iphone|ipad)\b", re.I),
    "chrome": re.compile(r"chrome|browser extension|\bextension\b", re.I),
    "habit": re.compile(r"habit|streak|routine", re.I),
}

# nameless/stealth entries carry no product signal for direction-finding
ANON_RE = re.compile(r"anonymous|stealth|hidden business|unnamed|undisclosed", re.I)


def get(params, key):
    qs = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{API}?{qs}", headers={"Authorization": f"Bearer {key}"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                print("rate limited, sleeping 65s", file=sys.stderr)
                time.sleep(65)
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
        kept = [it for it in batch if not ANON_RE.search(it.get("name") or "")]
        items.extend(kept)
        dropped = len(batch) - len(kept)
        print(f"{name}: page {page} -> {len(kept)} items" + (f" (dropped {dropped} anon)" if dropped else ""), file=sys.stderr)
        if not resp.get("meta", {}).get("hasMore"):
            break
        time.sleep(REQUEST_GAP_S)
    return items


def flags_for(item):
    text = " ".join(filter(None, [item.get("name"), item.get("description"), item.get("website")]))
    found = [f for f, pat in FLAG_PATTERNS.items() if pat.search(text)]
    if "apps.apple.com" in (item.get("website") or "") and "ios" not in found:
        found.append("ios")
    return found


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


def run_census(key, today):
    """Weekly per-category census: total verified startups and on-sale count."""
    path = DATA / "census.json"
    if not os.environ.get("FORCE_CENSUS") and path.exists():
        old = json.load(open(path, encoding="utf-8"))
        age = (date.fromisoformat(today) - date.fromisoformat(old["date"])).days
        if age < CENSUS_MAX_AGE_DAYS:
            print(f"census: {age}d old, reusing", file=sys.stderr)
            return old
    cats = os.environ.get("CENSUS_CATEGORIES")
    cats = cats.split(",") if cats else ALL_CATEGORIES
    census = {"date": today, "categories": {}}
    for c in cats:
        total = get({"category": c, "limit": 1}, key).get("meta", {}).get("total", 0)
        time.sleep(REQUEST_GAP_S)
        onsale = get({"category": c, "onSale": "true", "limit": 1}, key).get("meta", {}).get("total", 0)
        time.sleep(REQUEST_GAP_S)
        census["categories"][c] = {"total": total, "onSale": onsale}
        print(f"census: {c} total={total} onSale={onsale}", file=sys.stderr)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(census, f, ensure_ascii=False, separators=(",", ":"))
    return census


def build_heat(sections, census, today):
    """Per-category heat. User lens: presence in the growth pool (users voting
    with money). Capital lens: listing velocity and pricing (buyers voting)."""
    heat = {}
    now = datetime.now(timezone.utc)

    def bucket(cat):
        return heat.setdefault(cat, {"growing": 0, "growth": [], "listings7d": 0, "multiples": []})

    for it in sections.get("potential", []) + sections.get("focus", []):
        if not it.get("category"):
            continue
        b = bucket(it["category"])
        b["growing"] += 1
        if it.get("growth30d") is not None:
            b["growth"].append(it["growth30d"])
    seen_listing = set()
    for it in sections.get("recentListings", []) + sections.get("smallDeals", []):
        cat = it.get("category")
        if not cat:
            continue
        b = bucket(cat)
        # ignore junk pricing (absurd asking vs revenue) so tiny samples can't distort the median
        if it.get("multiple") is not None and 0 < it["multiple"] <= 50:
            b["multiples"].append(it["multiple"])
        listed = it.get("firstListedForSaleAt")
        if listed and it["slug"] not in seen_listing:
            seen_listing.add(it["slug"])
            try:
                dt = datetime.fromisoformat(listed.replace("Z", "+00:00"))
                if (now - dt).days < 7:
                    b["listings7d"] += 1
            except ValueError:
                pass

    cats = []
    for cat, b in heat.items():
        cats.append({
            "category": cat,
            "growing": b["growing"],
            "medianGrowth": round(median(b["growth"]), 1) if b["growth"] else None,
            "listings7d": b["listings7d"],
            "multipleSamples": len(b["multiples"]),
            "medianMultiple": round(median(b["multiples"]), 2) if len(b["multiples"]) >= 3 else None,
        })
    # census join (census keys are slugs; section categories are display names)
    slugify = lambda c: re.sub(r"[^a-z0-9]+", "-", c.lower()).strip("-")
    cmap = census.get("categories", {}) if census else {}
    alias = {"artificial-intelligence": "ai", "e-commerce": "ecommerce", "iot-hardware": "iot-hardware"}
    for c in cats:
        s = slugify(c["category"])
        s = alias.get(s, s)
        c["slug"] = s
        c["censusTotal"] = cmap.get(s, {}).get("total")
        c["censusOnSale"] = cmap.get(s, {}).get("onSale")
    cats.sort(key=lambda c: c["growing"], reverse=True)

    out = {"date": today, "generatedAt": now.isoformat(), "categories": cats}
    with open(DATA / "heat.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    # daily history (idempotent per day)
    hist_path = DATA / "heat.jsonl"
    rows = []
    if hist_path.exists():
        with open(hist_path, encoding="utf-8") as f:
            rows = [json.loads(l) for l in f if l.strip() and json.loads(l)["date"] != today]
    for c in cats:
        rows.append({"date": today, "cat": c["slug"], "growing": c["growing"],
                     "listings7d": c["listings7d"], "medianMultiple": c["medianMultiple"]})
    with open(hist_path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
    return out


def build_delta(today, merged, prev_by_slug, prev_date):
    delta = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "date": today,
        "prevDate": prev_date,
        "newListings": [],
        "delisted": [],
        "priceChanges": [],
        "mrrMoves": [],
        "milestones": [],
        "newcomers": [],
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
                "category": rec.get("category"), "flags": rec.get("flags", []),
            })
        if prev is None:
            # entered the tracked pool today (new growing small product)
            if rec.get("growth30d") and rec["growth30d"] > 0 and not rec.get("onSale"):
                delta["newcomers"].append({
                    "slug": slug, "name": rec["name"], "mrr": rec.get("mrr"),
                    "rev30": rec.get("rev30"), "growth30d": round(rec["growth30d"], 1),
                    "category": rec.get("category"), "flags": rec.get("flags", []),
                })
            continue
        if prev.get("onSale") and not rec.get("onSale"):
            delta["delisted"].append({"slug": slug, "name": rec["name"], "lastPrice": prev.get("price")})
        old_p, new_p = prev.get("price"), rec.get("price")
        if old_p and new_p and old_p != new_p and abs(pct_change(old_p, new_p)) >= 5:
            delta["priceChanges"].append({
                "slug": slug, "name": rec["name"], "from": old_p, "to": new_p,
                "pct": round(pct_change(old_p, new_p), 1),
            })
        old_m, new_m = prev.get("mrr"), rec.get("mrr")
        if old_m and new_m is not None:
            chg = pct_change(old_m, new_m)
            if chg is not None and abs(chg) >= 15 and abs(new_m - old_m) >= 200:
                delta["mrrMoves"].append({
                    "slug": slug, "name": rec["name"], "from": round(old_m), "to": round(new_m),
                    "pct": round(chg, 1),
                })
            for mark in MILESTONES:
                if old_m < mark <= new_m:
                    delta["milestones"].append({"slug": slug, "name": rec["name"], "crossed": mark,
                                                "direction": "up", "flags": rec.get("flags", [])})
                elif new_m < mark <= old_m:
                    delta["milestones"].append({"slug": slug, "name": rec["name"], "crossed": mark,
                                                "direction": "down", "flags": rec.get("flags", [])})

    delta["newListings"].sort(key=lambda x: x.get("mrr") or 0, reverse=True)
    delta["newcomers"].sort(key=lambda x: x.get("growth30d") or 0, reverse=True)
    delta["mrrMoves"].sort(key=lambda x: abs(x["pct"]), reverse=True)
    for k in ("newListings", "delisted", "priceChanges", "mrrMoves", "milestones", "newcomers"):
        delta[k] = delta[k][:20]
    return delta


def main():
    key = os.environ.get("TRUSTMRR_API_KEY")
    if not key:
        sys.exit("TRUSTMRR_API_KEY is not set")
    DATA.mkdir(exist_ok=True)
    today = date.today().isoformat()

    sections = {}
    for name, (params, pages) in SECTIONS_DAILY.items():
        sections[name] = fetch_section(name, params, pages, key)
        time.sleep(REQUEST_GAP_S)
    focus = []
    for cat in FOCUS_CATEGORIES:
        focus.extend(fetch_section(f"focus:{cat}", {**FOCUS_PARAMS, "category": cat}, FOCUS_PAGES, key))
        time.sleep(REQUEST_GAP_S)
    sections["focus"] = list({it["slug"]: it for it in focus}.values())

    merged = {}
    for items in sections.values():
        for it in items:
            rec = compact(it)
            rec["flags"] = flags_for(it)
            it["flags"] = rec["flags"]
            merged[it["slug"]] = rec

    if not merged:
        sys.exit("no data fetched, aborting without writing")

    census = run_census(key, today)
    heat = build_heat(sections, census, today)

    history = load_history()
    prev_dates = sorted({r["date"] for r in history if r["date"] != today})
    prev_date = prev_dates[-1] if prev_dates else None
    prev_by_slug = {r["slug"]: r for r in history if r["date"] == prev_date} if prev_date else {}

    history = [r for r in history if r["date"] != today]
    history.extend({"date": today, **rec} for rec in merged.values())
    with open(DATA / "snapshots.jsonl", "w", encoding="utf-8") as f:
        for r in history:
            f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")

    cutoff = sorted({r["date"] for r in history})[-90:]
    series = {}
    for r in history:
        if r["date"] not in cutoff:
            continue
        s = series.setdefault(r["slug"], {"name": r["name"], "points": []})
        s["points"].append([r["date"], r.get("mrr"), r.get("rev30")])
    for s in series.values():
        s["points"].sort()
    with open(DATA / "series-90d.json", "w", encoding="utf-8") as f:
        json.dump(series, f, ensure_ascii=False, separators=(",", ":"))

    with open(DATA / "latest.json", "w", encoding="utf-8") as f:
        json.dump({
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "date": today,
            "focusCategories": FOCUS_CATEGORIES,
            "sections": sections,
        }, f, ensure_ascii=False, separators=(",", ":"))

    delta = build_delta(today, merged, prev_by_slug, prev_date)
    with open(DATA / "delta.json", "w", encoding="utf-8") as f:
        json.dump(delta, f, ensure_ascii=False, separators=(",", ":"))

    print(f"done: {len(merged)} startups; heat cats={len(heat['categories'])}; prev={prev_date}", file=sys.stderr)


if __name__ == "__main__":
    main()
