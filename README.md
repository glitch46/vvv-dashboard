# VVV Unstaking Queue Dashboard

Static GitHub Pages dashboard that refreshes every 12 hours via GitHub Actions and Dune API.

## Data
- `data/daily.json`: daily initiated/unlocks/queue (30d)
- `data/summary.json`: current queue vs averages

## Refresh
GitHub Actions runs every 12 hours and overwrites the JSON files.
