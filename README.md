# VVV Unstaking Queue Dashboard

Static GitHub Pages dashboard that refreshes every 4 hours via GitHub Actions.

## Data
- `data/daily.json`: daily initiated/unlocks/queue (30d)
- `data/summary.json`: current queue vs averages
- `data/supply.json`: current supply breakdown used by pie chart
- `data/holders.json`: where circulating VVV is held (LP / exchanges / contracts / user wallets) plus hold-duration bands for user-held VVV
- `data/holders_state.json`: incremental FIFO state for the holder indexer (committed so backfill resumes across runs)

Staking and unstaking series come from Base RPC logs (`Staked` and `UnstakeInitiated` on sVVV). Dune is optional and only used for DEX volume when the API key can still execute queries.

## Holder distribution (`scripts/holders.py`)
Replays VVV `Transfer` logs via the Venice Crypto RPC (archive-backed) to compute where freely-circulating VVV sits:
- LP pools (curated Aerodrome/Uniswap address list from DexScreener)
- exchange wallets (curated label list, best-effort)
- contract addresses (any non-pool/non-exchange contract detected via `eth_getCode`)
- user wallets (EOAs)
User-held VVV is further split by the age of the current holding (FIFO per address, folded past the max-age bucket), with an average hold time as reference. Excludes staked, burned, and locked/treasury supply. The backfill is resumable and time-boxed per run (`VVV_TIME_BUDGET_SECONDS`).

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
