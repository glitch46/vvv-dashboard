#!/usr/bin/env python3
"""Holder-distribution & hold-age analytics for VVV on Base.

Replays VVV Transfer logs via the Venice Crypto RPC (archive-backed, 100 req/min,
10k-block getLogs chunks, idempotency keys) to compute, for the freely-circulating
supply:
  - category split: LP pools / exchange wallets / contract addresses / user wallets
  - hold-duration (age of current holding) for user-held VVV, FIFO per address
  - average hold time as a reference point

Resumable: state is persisted to data/holders_state.json (last_block + anchor
timestamp + per-address FIFO), so each run only processes new blocks and a
one-time backfill can span several scheduled runs. Only meaningful user-wallet
addresses keep a FIFO ledger (entries older than the max age bucket fold into a
legacy amount) to bound state size. Classification (eth_getCode) is deferred to
the end and only run for addresses with a meaningful balance.
"""

import json, os, time, datetime, urllib.request, urllib.error
from collections import deque

VENICE_API_KEY = os.environ.get('VENICE_API_KEY')
VENICE_RPC_URL = os.environ.get('VENICE_RPC_URL', 'https://api.venice.ai/api/v1/crypto/rpc/base-mainnet')
PUBLICNODE_RPC = os.environ.get('PUBLICNODE_RPC_URL', 'https://base.publicnode.com')
BASE_RPC = os.environ.get('BASE_RPC_URL', 'https://mainnet.base.org')
LOG_CHUNK = int(os.environ.get('VENICE_LOG_CHUNK', '10000'))
MIN_CHUNK = 1000
MAX_AGE_DAYS = int(os.environ.get('VVV_MAX_AGE_DAYS', '365'))
MIN_HOLD = float(os.environ.get('VVV_MIN_HOLD', '1.0'))
CLASSIFY_TOP_N = int(os.environ.get('VVV_CLASSIFY_TOP_N', '800'))
CHECKPOINT_EVERY = int(os.environ.get('VVV_CHECKPOINT_EVERY', '200'))
RATE_PER_MIN = 100.0
RESULT_SOFT_CAP = int(os.environ.get('VVV_LOG_SOFT_CAP', '9000'))

svvv = '0x321b7ff75154472B18EDb199033fF4D116F340Ff'
vvv = '0xACFE6019Ed1A7Dc6f7B508C02D1b04eC88cC21BF'
zero = '0x0000000000000000000000000000000000000000'
dead = '0x000000000000000000000000000000000000dEaD'
TRANSFER_TOPIC = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'

LOCKED_ADDRS = [
    '0x2d8cb8dc596dad0e1e34e2042e7ae6df93b11524',
    '0x4665883f3adb708f301ba75764d39ad0cd2a4d84',
    '0x4cb16d4153123a74bc724d161050959754f378d8',
    '0xb3c89592d84ae6adb6a1aa41515ac14ec822b175',
    '0xb6e08047320b4b4d943d7f1363776dddc6f4aa66'
]
LOCKED_ADDRS = [a.strip().lower() for a in LOCKED_ADDRS +
                os.environ.get('VVV_LOCKED_ADDRESSES', '').split(',') if a.strip()]

LP_POOLS = {
    'Aerodrome VVV/WETH': '0x01784ef301d79e4b2df3a21ad9a536d4cf09a5ce',
    'Aerodrome VVV/WETH v2': '0x7ec6c9d993d9832aa654593f2dbc21303650bc6c',
    'Aerodrome VVV/WETH v3': '0xdebaebe5b2729f7d62072b2ea40472d78d2c1626',
    'Uniswap V3 VVV/USDC': '0x67a11022b7b6ed66f81233f6c8ed6e48f7826530',
    'Aerodrome VVV/WETH v4': '0xa135b59fe221c0c8d441294f97f96fbc37bc9fbe',
    'Uniswap V3 VVV/WETH': '0x1d2bdb7117a5a7d7fe4c1d95681a92e4df13bb69',
    'Aerodrome VVV/WETH v5': '0x9f11c9e86ef74dd8bc6b3eefa9ff20a96b7f3ffe',
    'Aerodrome VVV/WETH v6': '0x46d398a5b33709877f50c8918a7ee96f1be1d7dd',
    'Uniswap V4 VVV/cbBTC': '0x36d7043ec113e9926b8f98392544a1b00b027556269bd35e2945403c69806f97',
    'Uniswap V4 VVV/USDC': '0x363fe74e05aa32b2cf6fe11e632cae4eabc9238c09d3ca626ef3afd414e2944e',
    'Uniswap V3 VVV/WETH alt': '0x8eaf39189c73819d3949cab2b1a7afce8e3ba0d9',
    'PancakeSwap VVV/cbBTC': '0xa1210d4563a4f8c6fd038a565ef988acb834ce57',
    'Aerodrome VVV/USDC': '0xc5de7f0514e7531dea5d71a30597c4cf131f533a',
    'Uniswap V3 VVV/USDC alt': '0x295cf9205a8763c7befc727135dda16da16db4e9',
    'Hydrex VVV/USDC': '0x02107bab40bba06ae979a15f67b5ad3853c5e116',
}
LP_ADDRS = {a.lower(): n for n, a in LP_POOLS.items()}

EXCHANGES = {
    'Binance 14': '0x28c6c06298d514db089934071355e5743bf21d60',
    'Binance 8': '0x21a31ee1afc51d94c2efccaa2092ad1028285549',
    'Coinbase Prime': '0x71660c4005ba85c37ccec55d0c4493e66fe775d3',
    'Coinbase 14': '0x503828976d22510aad0201ac7ec88293211d23da',
    'Bybit': '0xf89d7b9c864f589bbf53a82105107622b35eaa40',
    'OKX': '0x6cc5f688a315f3dc28a3671dd9b0825ff0a7c66b',
    'Kraken': '0xe853c56864a2ebe4576a807d26fdc4a0ada51919',
    'MEXC': '0x1b636bdcb02ddb92a28583eb2b8aa4bbc4dfe40c',
    'Bitget': '0x0639556f03714a74a3fee29f59cce065e51d48e8',
    'Gate.io': '0x0d0707963952f2fba59dd06f2b425ace40b492fe',
}
EXCHANGE_ADDRS = {a.lower(): n for n, a in EXCHANGES.items()}

_last_venice_ts = 0.0


def http_post_json(url, payload, headers, timeout=60):
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        raise RuntimeError(f'HTTP {e.code} for {url}: {body[:500]}') from e


def venice_rpc(payload, idempotency_key=None, timeout=120, retries=6):
    """Send a request to Venice RPC, respecting the 100 req/min cap + idempotency."""
    global _last_venice_ts
    if not VENICE_API_KEY:
        raise RuntimeError('VENICE_API_KEY not set')
    headers = {
        'Authorization': f'Bearer {VENICE_API_KEY}',
        'Content-Type': 'application/json',
    }
    if idempotency_key:
        headers['Idempotency-Key'] = idempotency_key
    for attempt in range(retries):
        now = time.time()
        gap = 60.0 / RATE_PER_MIN
        if now - _last_venice_ts < gap:
            time.sleep(gap - (now - _last_venice_ts))
        try:
            out = http_post_json(VENICE_RPC_URL, payload, headers, timeout=timeout)
            _last_venice_ts = time.time()
            if isinstance(out, dict) and out.get('error'):
                raise RuntimeError(f"Venice RPC error: {out['error']}")
            return out
        except Exception as e:
            _last_venice_ts = time.time()
            msg = str(e).lower()
            is_inflight = 'in flight' in msg or 'in-flight' in msg
            is_ratelimit = '429' in msg or 'rate limit' in msg or 'too many requests' in msg
            if attempt < retries - 1:
                # In-flight mutex and rate limits need longer, staggered waits.
                time.sleep(3 + 4 * attempt if is_inflight else 2 ** attempt)
                continue
            raise


def public_rpc(payload, timeout=60):
    last_err = None
    for url in (PUBLICNODE_RPC, BASE_RPC):
        for attempt in range(4):
            try:
                out = http_post_json(url, payload, {'Content-Type': 'application/json'}, timeout=timeout)
                if isinstance(out, dict) and out.get('error'):
                    raise RuntimeError(f"public RPC error: {out['error']}")
                return out
            except Exception as e:
                last_err = e
                time.sleep(1.5 * (attempt + 1))
    raise last_err or RuntimeError('All public RPCs failed')


def call_balance(address, block='latest'):
    padded = address.lower()[2:].rjust(64, '0')
    payload = {'jsonrpc': '2.0', 'method': 'eth_call',
               'params': [{'to': vvv.lower(), 'data': '0x70a08231' + padded}, block], 'id': 1}
    try:
        res = venice_rpc(payload)
    except Exception:
        res = public_rpc(payload)
    return int(res['result'], 16) / 1e18


def get_code(address, block='latest'):
    payload = {'jsonrpc': '2.0', 'method': 'eth_getCode',
               'params': [address.lower(), block], 'id': 1}
    try:
        res = venice_rpc(payload)
    except Exception:
        res = public_rpc(payload)
    code = res.get('result') or '0x'
    return code not in ('0x', '0X', '0x0')


def get_latest_block():
    try:
        res = venice_rpc({'jsonrpc': '2.0', 'method': 'eth_blockNumber', 'params': [], 'id': 1})
    except Exception:
        res = public_rpc({'jsonrpc': '2.0', 'method': 'eth_blockNumber', 'params': [], 'id': 1})
    num = int(res['result'], 16)
    try:
        bres = venice_rpc({'jsonrpc': '2.0', 'method': 'eth_getBlockByNumber',
                           'params': [hex(num), False], 'id': 1})
    except Exception:
        bres = public_rpc({'jsonrpc': '2.0', 'method': 'eth_getBlockByNumber',
                           'params': [hex(num), False], 'id': 1})
    return num, int(bres['result']['timestamp'], 16)


def get_block_ts(num):
    try:
        bres = venice_rpc({'jsonrpc': '2.0', 'method': 'eth_getBlockByNumber',
                           'params': [hex(num), False], 'id': 1})
    except Exception:
        bres = public_rpc({'jsonrpc': '2.0', 'method': 'eth_getBlockByNumber',
                           'params': [hex(num), False], 'id': 1})
    return int(bres['result']['timestamp'], 16)


def get_logs_venice_chunks(from_block, to_block):
    """Generator yielding (start_block, end_block, logs) chunks, adaptive size."""
    start = from_block
    chunk = LOG_CHUNK
    while start <= to_block:
        end = min(start + chunk - 1, to_block)
        payload = {
            'jsonrpc': '2.0', 'method': 'eth_getLogs', 'id': 1,
            'params': [{
                'address': vvv.lower(),
                'fromBlock': hex(start),
                'toBlock': hex(end),
                'topics': [TRANSFER_TOPIC]
            }]
        }
        key = f'log-{start}-{end}'
        try:
            res = venice_rpc(payload, idempotency_key=key)
            batch = res.get('result') or []
            if len(batch) >= RESULT_SOFT_CAP and chunk > MIN_CHUNK:
                chunk = max(chunk // 2, MIN_CHUNK)
                continue
            yield start, end, batch
            start = end + 1
        except Exception as e:
            msg = str(e).lower()
            if any(x in msg for x in ('range', 'limit', 'large', 'results', 'payload')) and chunk > MIN_CHUNK:
                chunk = max(chunk // 2, MIN_CHUNK)
                continue
            if not VENICE_API_KEY or 'key' in msg or 'auth' in msg:
                # No Venice key (or auth error): fall back to publicnode recent-only.
                try:
                    pres = public_rpc(payload)
                    pbatch = pres.get('result') or []
                    yield start, end, pbatch
                    start = end + 1
                    continue
                except Exception as pe:
                    raise pe from e
            raise


def find_first_transfer(latest_num):
    """Binary search for the first VVV Transfer block via Venice."""
    lo, hi = 1, latest_num
    probe = list(get_logs_venice_chunks(max(1, hi - LOG_CHUNK), hi))
    if not probe or not any(batch for _, _, batch in probe):
        return None
    lo, hi = 1, latest_num
    while lo < hi:
        mid = (lo + hi) // 2
        window = list(get_logs_venice_chunks(mid, min(mid + LOG_CHUNK - 1, hi)))
        if any(batch for _, _, batch in window):
            hi = mid
        else:
            lo = mid + 1
    return lo


def classify_address(a, kind_cache):
    """Known-list classification; contracts vs EOAs decided by code check."""
    if a in LP_ADDRS:
        return 'lp'
    if a in EXCHANGE_ADDRS:
        return 'exchange'
    if a in LOCKED_ADDRS:
        return 'locked'
    if a in (svvv.lower(), zero.lower(), dead.lower()):
        return 'excluded'
    if a in kind_cache:
        return kind_cache[a]
    kind = 'contract' if get_code(a) else 'user'
    kind_cache[a] = kind
    return kind


def write_state(state_path, last_block, last_ts, kind_cache, accounts):
    """Persist bounded state: kind/bal for all non-dust; FIFO only for meaningful users."""
    dust_cutoff = 0.01  # VVV
    state_accounts = {}
    for a, rec in accounts.items():
        if rec['bal'] < dust_cutoff and rec['kind'] in ('user', 'unknown'):
            continue  # drop dust EOAs to bound state size
        if rec['kind'] == 'user' and rec['bal'] >= MIN_HOLD:
            fifo = [list(x) for x in rec['fifo']]
        else:
            fifo = []
        state_accounts[a] = {
            'bal': round(rec['bal'], 8),
            'kind': rec['kind'],
            'fifo': fifo,
            'legacy': rec['legacy'],
        }
    state_out = {
        'last_block': last_block,
        'last_ts': last_ts,
        'kind_cache': kind_cache,
        'accounts': state_accounts,
    }
    os.makedirs(os.path.dirname(state_path) or '.', exist_ok=True)
    with open(state_path, 'w', encoding='utf-8') as f:
        json.dump(state_out, f, separators=(',', ':'))


def main():
    state_path = os.path.join('data', 'holders_state.json')
    out_path = os.path.join('data', 'holders.json')

    state = {}
    if os.path.exists(state_path):
        try:
            with open(state_path, encoding='utf-8') as f:
                state = json.load(f)
        except Exception as e:
            print(f'WARN: could not read state: {e}')

    accounts = state.get('accounts', {})
    # normalize fifo lists into deques
    for rec in accounts.values():
        rec.setdefault('fifo', [])
        if not isinstance(rec['fifo'], deque):
            rec['fifo'] = deque(rec['fifo'])
        rec.setdefault('legacy', 0.0)
        rec.setdefault('kind', 'user')
    last_block = int(state.get('last_block', 0) or 0)
    last_ts = float(state.get('last_ts', 0) or 0)
    kind_cache = state.get('kind_cache', {})

    latest_num, latest_ts = get_latest_block()
    print(f'latest_block={latest_num} latest_ts={latest_ts}')
    caught_up = True

    if last_block == 0:
        first = find_first_transfer(latest_num)
        if first is None:
            print('No transfers found; nothing to do.')
            return
        last_block = first
        try:
            last_ts = get_block_ts(first)
        except Exception:
            last_ts = latest_ts
        print(f'Backfilling from first transfer block {last_block} (ts={last_ts})')

    # Resolve a deterministic anchor timestamp so FIFO ages are consistent across runs.
    if last_ts == 0:
        try:
            last_ts = get_block_ts(last_block)
        except Exception:
            last_ts = latest_ts

    def get_acct(addr):
        if addr not in accounts:
            if addr in LP_ADDRS:
                kind = 'lp'
            elif addr in EXCHANGE_ADDRS:
                kind = 'exchange'
            elif addr in LOCKED_ADDRS:
                kind = 'locked'
            elif addr in (svvv.lower(), zero.lower(), dead.lower()):
                kind = 'excluded'
            else:
                kind = 'unknown'
            accounts[addr] = {'bal': 0.0, 'kind': kind, 'fifo': deque(), 'legacy': 0.0}
        return accounts[addr]

    def apply_logs(logs, sec_per_block, anchor_block, anchor_ts):
        for lg in logs:
            topics = lg.get('topics') or []
            if len(topics) < 3:
                continue
            frm = ('0x' + topics[1][-40:]).lower()
            to = ('0x' + topics[2][-40:]).lower()
            amt = int(lg.get('data') or '0x0', 16) / 1e18
            bnum = int(lg['blockNumber'], 16)
            ts = anchor_ts + (bnum - anchor_block) * sec_per_block

            src = get_acct(frm)
            dst = get_acct(to)
            src['bal'] -= amt
            dst['bal'] += amt
            if src['bal'] < 0:
                src['bal'] = 0.0  # fee-on-transfer / rounding clamp

            # maintain FIFO age ledger for addresses that are (or may be) user wallets.
            for acct, is_deposit, amount in ((src, False, amt), (dst, True, amt)):
                if acct['kind'] in ('lp', 'exchange', 'locked', 'excluded'):
                    continue
                if is_deposit:
                    acct['fifo'].append([ts, amount])
                else:
                    remaining = amount
                    if acct['legacy'] > 0:
                        take = min(acct['legacy'], remaining)
                        acct['legacy'] -= take
                        remaining -= take
                    q = acct['fifo']
                    while remaining > 1e-12 and q:
                        _, qamt = q[0]
                        if qamt <= remaining:
                            remaining -= qamt
                            q.popleft()
                        else:
                            q[0][1] = qamt - remaining
                            remaining = 0.0

            # fold entries older than the max age bucket into legacy to bound memory
            for acct in (src, dst):
                if acct['kind'] in ('lp', 'exchange', 'locked', 'excluded'):
                    continue
                q = acct['fifo']
                while q and q[0][0] < latest_ts - MAX_AGE_DAYS * 86400:
                    acct['legacy'] += q.popleft()[1]

    if last_block < latest_num:
        block_delta = latest_num - last_block
        sec_delta = latest_ts - last_ts
        sec_per_block = sec_delta / block_delta if block_delta > 0 else 2.0
        anchor_block, anchor_ts = last_block, last_ts

        budget_seconds = float(os.environ.get('VVV_TIME_BUDGET_SECONDS', '1200'))
        start_time = time.time()
        processed = 0
        caught_up = True
        chunk_count = 0
        for chunk_start, chunk_end, batch in get_logs_venice_chunks(last_block, latest_num):
            apply_logs(batch, sec_per_block, anchor_block, anchor_ts)
            processed += len(batch)
            last_block = chunk_end + 1
            chunk_count += 1
            # periodic checkpoint so a workflow timeout doesn't lose all progress
            if chunk_count % CHECKPOINT_EVERY == 0:
                try:
                    write_state(state_path, last_block, last_ts, kind_cache, accounts)
                    print(f'  checkpoint saved at block {last_block} ({processed} events)')
                except Exception as e:
                    print(f'  checkpoint save failed: {e}')
            if time.time() - start_time > budget_seconds:
                print(f'Time budget reached; pausing backfill at block {last_block}')
                caught_up = False
                break
        print(f'  processed {processed} transfer events; caught_up={caught_up}')
        if caught_up:
            last_block = latest_num
            last_ts = latest_ts
        else:
            # anchor the next run's interpolation to the last block we actually processed
            try:
                last_ts = get_block_ts(last_block - 1)
            except Exception:
                pass

    # fold + prune
    cutoff = latest_ts - MAX_AGE_DAYS * 86400
    for a in list(accounts.keys()):
        rec = accounts[a]
        if rec['bal'] < 1e-9:
            if rec['kind'] not in ('lp', 'exchange', 'contract', 'locked'):
                del accounts[a]
            continue

    # ---- classification (deferred), bounded to top-N holders by balance ----
    # Only meaningful addresses get an eth_getCode check (rate-limited); the
    # long tail of small balances defaults to 'user' (EOA-dominated).
    unknown = [(a, rec) for a, rec in accounts.items()
               if rec['kind'] == 'unknown' and rec['bal'] >= MIN_HOLD]
    unknown.sort(key=lambda x: -x[1]['bal'])
    for a, rec in accounts.items():
        if rec['kind'] != 'unknown':
            continue
        if rec['bal'] < MIN_HOLD:
            rec['kind'] = 'user'
    for a, rec in unknown[:CLASSIFY_TOP_N]:
        rec['kind'] = classify_address(a, kind_cache)
        if rec['kind'] != 'user':
            rec['fifo'] = deque()
    for a, rec in unknown[CLASSIFY_TOP_N:]:
        rec['kind'] = 'user'  # below top-N: assume EOA user wallet

    # fold old FIFO entries into legacy for user accounts (only meaningful ones)
    for a, rec in accounts.items():
        if rec['kind'] != 'user':
            continue
        q = rec['fifo']
        while q and q[0][0] < cutoff:
            rec['legacy'] += q.popleft()[1]

    # ---- build aggregates ----
    cat = {'lp': 0.0, 'exchange': 0.0, 'contract': 0.0, 'user': 0.0, 'locked': 0.0, 'excluded': 0.0}
    lp_detail, exch_detail, contract_detail = {}, {}, {}
    user_total = 0.0

    for a, rec in accounts.items():
        kind = rec['kind']
        bal = rec['bal']
        if kind == 'excluded':
            cat['excluded'] += bal
        elif kind == 'locked':
            cat['locked'] += bal
        elif kind == 'lp':
            cat['lp'] += bal
            lp_detail[LP_ADDRS.get(a, a)] = bal
        elif kind == 'exchange':
            cat['exchange'] += bal
            exch_detail[EXCHANGE_ADDRS.get(a, a)] = bal
        elif kind == 'contract':
            cat['contract'] += bal
            contract_detail[a] = bal
        else:
            cat['user'] += bal
            user_total += bal

    # ensure LP / exchange live balances exact even if never seen in transfers
    for a, name in LP_ADDRS.items():
        if a not in accounts:
            try:
                b = call_balance(a)
            except Exception:
                b = 0
            if b > 1e-9:
                cat['lp'] += b
                lp_detail[name] = lp_detail.get(name, 0) + b
    for a, name in EXCHANGE_ADDRS.items():
        if a not in accounts:
            try:
                b = call_balance(a)
            except Exception:
                b = 0
            if b > 1e-9:
                cat['exchange'] += b
                exch_detail[name] = exch_detail.get(name, 0) + b

    # ---- user hold-age bands (FIFO) ----
    bands = {'<24h': 0.0, '1-7d': 0.0, '7-30d': 0.0, '30-90d': 0.0,
             '90-180d': 0.0, '180-365d': 0.0, '>365d': 0.0}
    age_wsum, age_wsupply = 0.0, 0.0
    user_holders = 0
    for a, rec in accounts.items():
        if rec['kind'] != 'user' or rec['bal'] < MIN_HOLD:
            continue
        user_holders += 1
        for ts, amt in rec['fifo']:
            age_days = max(0.0, (latest_ts - ts) / 86400.0)
            if age_days < 1:
                bands['<24h'] += amt
            elif age_days < 7:
                bands['1-7d'] += amt
            elif age_days < 30:
                bands['7-30d'] += amt
            elif age_days < 90:
                bands['30-90d'] += amt
            elif age_days < 180:
                bands['90-180d'] += amt
            elif age_days < 365:
                bands['180-365d'] += amt
            else:
                bands['>365d'] += amt
            age_wsum += amt * age_days
            age_wsupply += amt
        if rec['legacy'] > 0:
            bands['>365d'] += rec['legacy']
            age_wsum += rec['legacy'] * MAX_AGE_DAYS
            age_wsupply += rec['legacy']

    avg_hold_days = (age_wsum / age_wsupply) if age_wsupply else 0.0

    top_users = []
    for a, rec in accounts.items():
        if rec['kind'] == 'user' and rec['bal'] >= 1e-9:
            top_users.append((a, rec['bal']))
    top_users.sort(key=lambda x: -x[1])
    top_users = [{'address': a, 'balance': round(b, 2)} for a, b in top_users[:50]]

    summary = {
        'as_of': datetime.datetime.utcfromtimestamp(latest_ts).strftime('%Y-%m-%d %H:%M UTC'),
        'latest_block': latest_num,
        'backfill_complete': caught_up,
        'circulating_total': round(sum(v for k, v in cat.items() if k not in ('excluded', 'locked')), 2),
        'categories': {k: round(v, 2) for k, v in cat.items()},
        'lp_pools': {k: round(v, 2) for k, v in sorted(lp_detail.items(), key=lambda x: -x[1])},
        'exchanges': {k: round(v, 2) for k, v in sorted(exch_detail.items(), key=lambda x: -x[1])},
        'contracts': {k: round(v, 2) for k, v in sorted(contract_detail.items(), key=lambda x: -x[1])[:40]},
        'user_hold_bands': {k: round(v, 2) for k, v in bands.items()},
        'avg_hold_days': round(avg_hold_days, 1),
        'user_holder_count': user_holders,
        'top_users': top_users,
    }

    os.makedirs('data', exist_ok=True)
    if caught_up:
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(summary, f, indent=2)
    elif os.path.exists(out_path):
        print('Backfill not complete; keeping previous holders.json')
    else:
        # First run, not yet caught up: publish a placeholder so the page renders
        # a "warming up" state instead of stale partial numbers.
        placeholder = {
            'as_of': datetime.datetime.utcfromtimestamp(latest_ts).strftime('%Y-%m-%d %H:%M UTC'),
            'latest_block': latest_num,
            'backfill_complete': False,
            'message': 'Holder analysis is indexing full transfer history; the snapshot will appear once the backfill is complete (typically within a few scheduled runs).',
        }
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(placeholder, f, indent=2)

    # persist bounded state via shared helper (also used for periodic checkpoints)
    write_state(state_path, last_block, last_ts, kind_cache, accounts)

    print('Holders summary:')
    for k, v in summary['categories'].items():
        print(f'  {k}: {v:,.0f}')
    print(f'  user_hold_bands: {summary["user_hold_bands"]}')
    print(f'  avg_hold_days: {summary["avg_hold_days"]}')
    print(f'  user_holder_count: {summary["user_holder_count"]}')


if __name__ == '__main__':
    main()
