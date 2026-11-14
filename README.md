# VVV Unstaking Queue Dashboard

Static GitHub Pages dashboard that refreshes every 4 hours via GitHub Actions.

## Data
- `data/daily.json`: daily initiated/unlocks/queue (30d)
- `data/summary.json`: current queue vs averages
- `data/supply.json`: current supply breakdown used by pie chart
- `data/holders.json`: where circulating VVV is held (LP / contracts / user wallets) plus hold-duration bands for user-held VVV
- `data/holders_daily.json`: 30-day series of circulating VVV split by LP / contracts / user wallets
- `data/holders_state.json`: incremental FIFO state for the holder indexer (committed so backfill resumes across runs)
- `data/unlock.json`: unlocked-VVV sell-pressure snapshot (VVV released to user wallets by `finalizeUnstake`, still held, bucketed by time since unlock)
- `data/unlock_state.json`: incremental FIFO state for the unlock-flow indexer

Staking and unstaking series come from Base RPC logs (`Staked` and `UnstakeInitiated` on sVVV). Dune is optional and only used for DEX volume when the API key can still execute queries.

## Holder distribution (`scripts/holders.py`)
Replays VVV `Transfer` logs via the Venice Crypto RPC (archive-backed) to compute where freely-circulating VVV sits:
- LP pools (curated Aerodrome/Uniswap address list from DexScreener)
- contract addresses (any non-pool contract detected via `eth_getCode`; includes exchange hot wallets)
- user wallets (EOAs)
User-held VVV is further split by the age of the current holding (FIFO per address, folded past the max-age bucket), with an average hold time as reference. Excludes staked, burned, and locked/treasury supply. The backfill is resumable and time-boxed per run (`VVV_TIME_BUDGET_SECONDS`).

## Unlocked-VVV sell pressure (`scripts/unlock_flow.py`)
Replays the same VVV `Transfer` logs and maintains a per-wallet FIFO of "unlocked VVV" — deposits where the sender is the sVVV staking contract (i.e. `finalizeUnstake` released them). Any outgoing transfer consumes from that FIFO oldest-first, so the remainder is unlocked VVV still sitting in user wallets. Output buckets it by time since unlock (`<24h`, `1-7d`, `7-30d`, `30-90d`, `>90d`) as a sell-pressure signal, plus the average days from unlock to first sale as a reference.

## Refresh
GitHub Actions runs every 4 hours and overwrites the JSON files.

### Largest Unstakers Activity

`scripts/unstaker_activity.py` independently generates `data/top_unstakers.json`
(schema version 2). Every scheduled refresh ranks the **top seven wallets by total
unstake initiations over the rolling last 30 days**, then follows each selected wallet's **all available
VVV history**. `data/unstaker_activity_state.json` checkpoints both log scanning and
transaction verification so the first backfill can span several refreshes. When
the ranking window is expanded, older initiation events are backfilled before a
complete 30-day ranking is published; the previous ranking remains labeled with
its actual window until then.
The activity step has its own 15-minute budget and does not block the existing
stake, holder or sell-pressure charts. Incomplete history is shown as indexing,
not as zero sales. Last verified output is retained if ranking fails.

For the initial backfill, the **Backfill Unstaker Activity** workflow can run
independently of the slower full dashboard refresh. It uses the repository's
existing `ALCHEMY_API_KEY` for paginated Base ERC20 transfer discovery and
`VENICE_API_KEY` for archive receipts, blocks, transactions and balance reads.
Each indexed transaction is checked against its exact VVV receipt logs before
it enters the replay. Indexed amounts/labels are never treated as evidence of
a claim or sale. Receipts, page cursors and per-wallet progress are persisted;
after backfill, only new blocks need scanning. If the indexed API is unavailable,
the worker falls back to the resumable archive RPC path. Use
`VVV_ACTIVITY_HISTORY_SOURCE=rpc` to force that fallback.
Receipt-matched block timestamps from the indexed API avoid redundant archive
block reads; a timestamp conflicting with an existing RPC read is rejected.

The default history lookback is `VVV_ACTIVITY_HISTORY_DAYS=all`; numeric values
such as `365` or `90` can be supplied for a bounded window. Expanding the window
backfills the missing older history rather than reusing a recent-only cache as
if it were complete. Each wallet carries its actual verified coverage while an
older backfill is pending. The chart retains the last verified snapshot.
Wallets with no verified claims still show a separate historical context and
transaction table (staking deposits, other staking payments and transfers),
without treating those activities as unstake sales.
The manual backfill's `rpc_provider=alchemy` option uses Alchemy's faster archive
read pace, retaining Venice as a fallback. `venice`/`auto` preserve Venice-first
verification. Both paths require the same receipt evidence and closing-balance
reconciliation. Queued refreshes check out the latest master checkpoint when
they start so a preceding backfill's progress is not lost.

- Timing starts at an **actual verified unstake claim**, not cooldown expiry.
  Reward transfers from sVVV during an initiation/stake are not assumed claims.
- Non-overlapping buckets: under 24h, 24–48h, 48–72h, 3–7 days, over 7 days,
  plus the claim balance **still in the original wallet**.
- Stacked series separate verified swaps, transfers to other wallets, other
  contract transfers, re-staking and still-held VVV. An exchange deposit or
  arbitrary transfer is never called a sale.
- Supported sale evidence: wallet-signed V2, V3 and Slipstream swaps, verified
  pool token identities, a traced VVV input and a non-VVV ERC20 output from
  that pool back to the wallet in the same receipt. Unsupported routing,
  native-ETH outputs and V4 swaps remain **unverified transfers**.
- Claim attribution is FIFO over **all** wallet VVV (including its historical
  opening balance and unrelated inflows). It is an accounting convention for
  fungible tokens, not proof of ownership or intent. The replay must reconcile
  with the on-chain closing balance before history is marked complete.
- `VVV_ACTIVITY_BUDGET_SECONDS` controls the per-run time budget;
  `VVV_ACTIVITY_LOG_CHUNK` is capped at 40,000 blocks to avoid oversized queries.

Run the isolated evidence/accounting tests with:
`python -m unittest discover -s scripts -p "test_unstaker_activity.py" -v`.

## Primary-position Telegram investigations

`.github/workflows/unstaker-alert.yml` checks the primary VVV position every
**10 minutes** (GitHub may delay scheduled runs). It invokes
`scripts/notify_unstaker.py`, with investigation/tracking in
`scripts/position_monitor.py`. It reuses the existing `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_CHAT_ID`, `ALCHEMY_API_KEY` and `VENICE_API_KEY` repository secrets.

`data/position_watch.json` pins these two roots independently of dashboard rankings:

- Original large unstaker: `0xa87597a0aad36554a1dec8a09cc4724d99e4d2f8`
- 400,000 VVV recipient: `0x1e49568d70acbcad02a08c14ff3abf92efe8aae4`

Rules:

- A movement alert requires **at least 500 VVV OR an estimated $10,000**. The
  comparison is inclusive and uses the largest qualifying wallet movement in
  one transaction, not a sum across separate transactions. Incoming/outgoing
  legs and staking events are not added together to inflate the threshold.
  Approvals, price changes alone, zero-value token events and smaller movements
  do not fire movement alerts.
- USD value is a timestamped observation-time estimate, **not execution
  proceeds**. The highest-liquidity qualifying Base VVV market on DexScreener
  supplies the quote (minimum $100,000 pool liquidity). Cached quotes expire
  after 30 minutes. Token-based alerts continue during price outages; events
  without a quote remain available for valuation retry.
- Follow **two onward wallet transfers from either pinned root**. The roots'
  mutual transfer consumes no onward hop. Recipient accounting uses FIFO over
  all VVV, including opening/unrelated balances, and follows only the attributed
  part of an outflow. Reusing an address does not reset the hop count. Contracts,
  pools, staking/token contracts and burns are terminal endpoints; cross-chain
  continuation is not indexed. A link is fund-flow evidence, not ownership.
- Reports/alerts include sourced exchange attribution. `address_labels` holds
  dated, manually checked public-explorer labels; unlabeled recipients are also
  checked against Etherscan titles, with 24-hour caching. Cross-chain exact-address
  labels identify their source chain explicitly, not proof of current Base custody.
  The observed `0x9d57...7831` deposit is tagged ByBit Deposit; its
  `0xbaed...439f` sweep destination is tagged Bybit: Hot Wallet 6. The latter is a
  terminal omnibus endpoint: unrelated exchange inventory/activity is not treated
  as this whale's position. Sender ownership is never inferred from a deposit.
  Changed source-checked attribution queues one investigation update immediately,
  independently of the next daily report; unchanged labels do not repeat it.
- The initial investigation starts at the verified 500,000 VVV initiation in
  block `53989536`; the recipient's own history starts at its first linked receipt
  in block `57150775`. Four seed transactions are reverified against live receipts.
  Recent root activity and newly discovered recipients are checked first; old
  history is resumable and time-boxed. Partial history is explicitly labeled.
  Completed wallet histories must reconcile opening balance + transfers against
  their pinned on-chain closing balance; mismatches trigger a rescan.
- One daily Telegram investigation is queued at the first successful poll after
  **12:00 UTC**, plus an initial coverage report and a completed baseline report.
  Reports include below-threshold VVV activity, staking, claims, rewards,
  approvals, transaction evidence, balances, pending unstake/cooldown and coverage.
  Alchemy additionally discovers other ERC20/NFT and external/internal ETH
  activity. Other-asset indexing has separate retry cursors and never silently
  claims complete coverage after an outage. Arbitrary calls without indexed
  transfers may not be discoverable. Only receipt-verified supported swaps are
  called sales; indexed labels are context only.
- `.alert-state/position.json` holds receipt history, separate live/backfill
  cursors, attribution openings, daily markers and a paginated Telegram outbox.
  Versioned Actions caches restore/save it, including on failure. The old
  `.alert-state/state.json` alert checkpoint is independent. An alert crossing
  both roots is queued once. Pages beyond the delivery limit stay queued;
  failed sends are retried. Delivery is at-least-once: a crash after Telegram
  accepts a page but before its checkpoint is saved can repeat that page.
  Cache loss starts a labeled new baseline rather than trusting an absent cursor.

Verification:

```text
python -m unittest discover -s scripts -p "test_position_monitor.py" -v
python scripts/notify_unstaker.py --dry-run --test
python scripts/notify_unstaker.py --dry-run
```

`--dry-run` makes no state writes and sends no Telegram messages. `--test`
without `--dry-run` sends a configuration test to the existing destination.
`--config` and `--state` allow isolated configurations/checkpoints.
`VVV_ALERT_CONFIRMATIONS` defaults to a 64-block head lag (not L1 finality);
`VVV_ALERT_BUDGET_SECONDS` defaults to 420 seconds. Historical backfill gets
at most 120 seconds per run so it does not displace live checks and reporting.
Position RPC prefers the dashboard's public archive endpoints for bounded log
queries, with authenticated RPC fallbacks. Alchemy remains the independent
transfer-index source; slow authenticated wildcard-log queries cannot take
precedence over the working public archive path.

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
