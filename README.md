# VVV Unstaking Queue Dashboard

Static GitHub Pages dashboard that refreshes every 4 hours via GitHub Actions.

## Data
- `data/daily.json`: daily initiated/unlocks/queue (30d)
- `data/summary.json`: current queue vs averages
- `data/supply.json`: current supply breakdown used by pie chart

## Refresh
GitHub Actions runs every 4 hours and overwrites the JSON files.

## Supply data (Venice Crypto RPC on Base)

The supply pie chart uses Venice Crypto RPC against `base-mainnet`.

Required secrets:

- `VENICE_API_KEY`: Venice API key used for `/crypto/rpc/base-mainnet` calls.
- `VVV_LOCKED_ADDRESSES`: optional extra comma-separated addresses to classify as locked (in addition to hard-coded top holder contracts).
- `VVV_BURN_ADDRESSES`: optional comma-separated burn addresses (in addition to `0x000...0000` and `0x000...dEaD`).

RPC calls used:

- Total supply: `eth_call` with `totalSupply()` selector (`0x18160ddd`) on the VVV token.
- Locked supply: sum of `eth_call` `balanceOf(address)` across locked addresses.
- Staked supply: `eth_call` `balanceOf(sVVV)`.
- Burned supply: `eth_call` `balanceOf(address)` at `0x000...0000`, `0x000...dEaD`, plus any extra burn addresses.
