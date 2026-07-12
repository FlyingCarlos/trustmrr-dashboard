# TrustMRR Tracker

Daily snapshot of [TrustMRR](https://trustmrr.com) verified startup revenue data, rendered as a static dashboard on GitHub Pages.

Founder lens: tracks small-but-promising products (not top-revenue incumbents), five focus categories, and per-category market heat.

- **Collection**: `.github/workflows/daily.yml` runs `scripts/fetch.py` once a day (08:30 UTC) with a `TRUSTMRR_API_KEY` repository secret, and commits the results to `data/`.
- **Tracked segments** (all for-sale only, so every item carries an asking price / multiple): potential pool ($200–$15k monthly revenue, fastest growing, all categories), focus categories (mobile-apps / games / health-fitness / productivity / utilities, $100–$20k), recent listings, small deals (asking ≤ $50k). Items are keyword-flagged `ios` / `chrome` / `habit`, anonymous/stealth entries are dropped, and a traction gate (MRR ≥ $50, or active subscribers, or rev ≥ $1k/mo) filters stagnant side projects.
- **Market heat**: user lens = category share of the growth pool; capital lens = 7-day listing velocity + median asking multiple. A weekly census (per-category totals and on-sale counts) refreshes every ~6 days; daily heat is appended to `data/heat.jsonl`.
- **Dashboard**: `index.html` is fully static — it fetches `data/*.json` client-side. Heat bars + table, sortable tables, flag chips, deal filters, 90-day sparklines (MRR, falling back to revenue), and a "today's changes" feed (milestones at $1k/$5k/$10k/$25k/$50k/$100k MRR, pool newcomers, new listings, delistings, price changes, MRR moves).
- **History**: `data/snapshots.jsonl` is the append-only archive, one line per startup per day.

All amounts are USD. Data belongs to TrustMRR and the founders who verified it; this is a personal tracking view.
