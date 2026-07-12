# TrustMRR Tracker

Daily snapshot of [TrustMRR](https://trustmrr.com) verified startup revenue data, rendered as a static dashboard on GitHub Pages.

- **Collection**: `.github/workflows/daily.yml` runs `scripts/fetch.py` once a day (08:30 UTC) with a `TRUSTMRR_API_KEY` repository secret, and commits the results to `data/`.
- **Tracked segments**: top 100 by 30-day revenue, growth movers (rev ≥ $1k), top 100 on-sale best deals, on-sale startups with MRR ≥ $2k.
- **Dashboard**: `index.html` is fully static — it fetches `data/*.json` client-side. Sortable tables, deal filters, 90-day MRR sparklines, and a "today's changes" feed (new listings, delistings, price changes, rank moves, MRR milestones).
- **History**: `data/snapshots.jsonl` is the append-only archive, one line per startup per day.

All amounts are USD. Data belongs to TrustMRR and the founders who verified it; this is a personal tracking view.
