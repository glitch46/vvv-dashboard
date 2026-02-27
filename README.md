# VVV Unstaking Queue Dashboard

Static GitHub Pages dashboard that refreshes every 12 hours via GitHub Actions and Dune API.

## Data
- `data/daily.json`: daily initiated/unlocks/queue (30d)
- `data/summary.json`: current queue vs averages
- `data/supply.json`: current supply breakdown used by pie chart

## Refresh
GitHub Actions runs every 12 hours and overwrites the JSON files.

## Supply data (Basescan / Etherscan V2)

The supply pie chart uses Basescan via Etherscan V2 endpoints.

Required secrets:

- `ETHERSCAN_API_KEY`: Etherscan API key (v2 works for Base with `chainid=8453`).
- `VVV_LOCKED_ADDRESSES`: optional extra comma-separated addresses to classify as locked (in addition to hard-coded top holder contracts).
- `VVV_BURN_ADDRESSES`: optional comma-separated burn addresses (in addition to `0x000...0000` and `0x000...dEaD`).

Endpoints used:

- Total supply: `module=stats&action=tokensupply&contractaddress=VVV`.
- Locked supply: sum of `module=account&action=tokenbalance` for each locked address.
- Staked supply: token balance at sVVV address.
- Burned supply: token balance at `0x000...0000`, `0x000...dEaD`, plus any extra burn addresses.
