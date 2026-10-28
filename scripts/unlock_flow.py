#!/usr/bin/env python3
"""Unlocked-VVV sell-pressure analytics for VVV on Base.

Unstake lifecycle: user stakes VVV -> sVVV, calls initiateUnstake (7d cooldown),
then finalizeUnstake. On finalize, VVV is transferred from the sVVV contract back
to the user's wallet and becomes freely sellable.

This script replays VVV Transfer logs (same Venice RPC + idempotency cache as
holders.py) and maintains a per-wallet FIFO of "unlocked VVV" (deposits where
from == sVVV) and consumes from it oldest-first on any outgoing transfer. The
remainder is the unlocked VVV still sitting in user wallets, bucketed by how long
ago it unlocked:

    <24h / 1-7d / 7-30d / 30-90d / >90d

Plus the average time from unlock to first sale as a reference point. Excludes
LP pools, contracts, exchanges, locked and burned addresses (user wallets only).

State is persisted to data/unlock_state.json (resumable + time-boxed like the
holder indexer) and the summary goes to data/unlock.json.
"""

import json, os, time, datetime, urllib.request, urllib.error
from collections import deque

VENICE_API_KEY = os.environ.get('VENICE_API_KEY')
VENICE_RPC_URL = os.environ.get('VENICE_RPC_URL', 'https://api.venice.ai/api/v1/crypto/rpc/base-mainnet')
PUBLICNODE_RPC = os.environ.get('PUBLICNODE_RPC_URL', 'https://base.publicnode.com')
BASE_RPC = os.environ.get('BASE_RPC_URL', 'https://mainnet.base.org')
LOG_CHUNK = int(os.environ.get('VENICE_LOG_CHUNK', '10000'))
MIN_CHUNK = 1000
RATE_PER_MIN = 100.0
RESULT_SOFT_CAP = int(os.environ.get('VVV_LOG_SOFT_CAP', '9000'))
CHECKPOINT_EVERY = int(os.environ.get('VVV_CHECKPOINT_EVERY', '200'))
MAX_AGE_DAYS = 90

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

LP_ADDRS = {
    '0x01784ef301d79e4b2df3a21ad9a536d4cf09a5ce',
    '0x7ec6c9d993d9832aa654593f2dbc21303650bc6c',
    '0xdebaebe5b2729f7d62072b2ea40472d78d2c1626',
    '0x67a11022b7b6ed66f81233f6c8ed6e48f7826530',
    '0xa135b59fe221c0c8d441294f97f96fbc37bc9fbe',
    '0x1d2bdb7117a5a7d7fe4c1d95681a92e4df13bb69',
    '0x9f11c9e86ef74dd8bc6b3eefa9ff20a96b7f3ffe',
    '0x46d398a5b33709877f50c8918a7ee96f1be1d7dd',
    '0x36d7043ec113e9926b8f98392544a1b00b027556269bd35e2945403c69806f97',
    '0x363fe74e05aa32b2cf6fe11e632cae4eabc9238c09d3ca626ef3afd414e2944e',
    '0x8eaf39189c73819d3949cab2b1a7afce8e3ba0d9',
    '0xa1210d4563a4f8c6fd038a565ef988acb834ce57',
    '0xc5de7f0514e7531dea5d71a30597c4cf131f533a',
    '0x295cf9205a8763c7befc727135dda16da16db4e9',
    '0x02107bab40bba06ae979a15f67b5ad3853c5e116',
}
LP_ADDRS = {a.lower() for a in LP_ADDRS}

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
            if attempt < retries - 1:
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
    """Generator yielding (start, end, logs) chunks, adaptive size, cache-backed."""
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
                try:
                    pres = public_rpc(payload)
                    yield start, end, pres.get('result') or []
                    start = end + 1
                    continue
                except Exception as pe:
                    raise pe from e
            raise


def find_first_transfer(latest_num):
    lo, hi = 1, latest_num
    probe = list(get_logs_venice_chunks(max(1, hi - LOG_CHUNK), hi))
    if not probe or not any(b for _, _, b in probe):
        return None
    lo, hi = 1, latest_num
    while lo < hi:
        mid = (lo + hi) // 2
        window = list(get_logs_venice_chunks(mid, min(mid + LOG_CHUNK - 1, hi)))
        if any(b for _, _, b in window):
            hi = mid
        else:
            lo = mid + 1
    return lo


def write_state(state_path, last_block, last_ts, wallets):
    out = {
        'last_block': last_block,
        'last_ts': last_ts,
        'wallets': {
            a: {
                'unlocked': [list(x) for x in rec['unlocked']],
                'legacy': rec['legacy'],
                'sold_amt': rec['sold_amt'],
                'sold_age_sum': rec['sold_age_sum'],
            }
            for a, rec in wallets.items()
        },
    }
    os.makedirs(os.path.dirname(state_path) or '.', exist_ok=True)
    with open(state_path, 'w', encoding='utf-8') as f:
        json.dump(out, f, separators=(',', ':'))


def main():
    state_path = os.path.join('data', 'unlock_state.json')
    out_path = os.path.join('data', 'unlock.json')

    state = {}
    if os.path.exists(state_path):
        try:
            with open(state_path, encoding='utf-8') as f:
                state = json.load(f)
        except Exception as e:
            print(f'WARN: could not read unlock state: {e}')

    wallets = state.get('wallets', {})
    for rec in wallets.values():
        rec.setdefault('unlocked', [])
        if not isinstance(rec['unlocked'], deque):
            rec['unlocked'] = deque(rec['unlocked'])
        rec.setdefault('legacy', 0.0)
        rec.setdefault('sold_amt', 0.0)
        rec.setdefault('sold_age_sum', 0.0)
    last_block = int(state.get('last_block', 0) or 0)
    last_ts = float(state.get('last_ts', 0) or 0)

    latest_num, latest_ts = get_latest_block()
    print(f'latest_block={latest_num} latest_ts={latest_ts}')

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
        print(f'Backfilling unlock flow from block {last_block}')

    if last_ts == 0:
        try:
            last_ts = get_block_ts(last_block)
        except Exception:
            last_ts = latest_ts

    def get_wallet(addr):
        if addr not in wallets:
            wallets[addr] = {'unlocked': deque(), 'legacy': 0.0,
                             'sold_amt': 0.0, 'sold_age_sum': 0.0}
        return wallets[addr]

    def is_user_wallet(addr):
        a = addr.lower()
        if a in LP_ADDRS:
            return False
        if a in LOCKED_ADDRS:
            return False
        if a in (svvv.lower(), zero.lower(), dead.lower()):
            return False
        return True

    if last_block < latest_num:
        block_delta = latest_num - last_block
        sec_delta = latest_ts - last_ts
        sec_per_block = sec_delta / block_delta if block_delta > 0 else 2.0
        anchor_block, anchor_ts = last_block, last_ts
        budget_seconds = float(os.environ.get('VVV_TIME_BUDGET_SECONDS', '5400'))
        start_time = time.time()
        caught_up = True
        chunk_count = 0
        for chunk_start, chunk_end, batch in get_logs_venice_chunks(last_block, latest_num):
            for lg in batch:
                topics = lg.get('topics') or []
                if len(topics) < 3:
                    continue
                frm = ('0x' + topics[1][-40:]).lower()
                to = ('0x' + topics[2][-40:]).lower()
                amt = int(lg.get('data') or '0x0', 16) / 1e18
                bnum = int(lg['blockNumber'], 16)
                ts = anchor_ts + (bnum - anchor_block) * sec_per_block

                # Unlock deposit: VVV leaves the sVVV contract to a user wallet.
                if frm == svvv.lower():
                    if is_user_wallet(to) and amt > 1e-12:
                        w = get_wallet(to)
                        w['unlocked'].append([ts, amt])

                # Any outgoing transfer from a user wallet consumes unlocked VVV first.
                if is_user_wallet(frm) and amt > 1e-12:
                    w = get_wallet(frm)
                    remaining = amt
                    if w['legacy'] > 0:
                        take = min(w['legacy'], remaining)
                        w['legacy'] -= take
                        # legacy is older than the max bucket; age ~ MAX_AGE_DAYS
                        w['sold_amt'] += take
                        w['sold_age_sum'] += take * MAX_AGE_DAYS
                        remaining -= take
                    q = w['unlocked']
                    while remaining > 1e-12 and q:
                        uts, qamt = q[0]
                        if qamt <= remaining:
                            remaining -= qamt
                            q.popleft()
                            age_days = max(0.0, (latest_ts - uts) / 86400.0)
                            w['sold_amt'] += qamt
                            w['sold_age_sum'] += qamt * age_days
                        else:
                            q[0][1] = qamt - remaining
                            age_days = max(0.0, (latest_ts - uts) / 86400.0)
                            w['sold_amt'] += remaining
                            w['sold_age_sum'] += remaining * age_days
                            remaining = 0.0

                # Fold entries older than the max age bucket into legacy to bound memory.
                cutoff = latest_ts - MAX_AGE_DAYS * 86400
                if frm == svvv.lower() and is_user_wallet(to):
                    w = get_wallet(to)
                    while w['unlocked'] and w['unlocked'][0][0] < cutoff:
                        w['legacy'] += w['unlocked'].popleft()[1]

            last_block = chunk_end + 1
            chunk_count += 1
            if chunk_count % CHECKPOINT_EVERY == 0:
                try:
                    write_state(state_path, last_block, last_ts, wallets)
                    print(f'  checkpoint saved at block {last_block}')
                except Exception as e:
                    print(f'  checkpoint save failed: {e}')
            if time.time() - start_time > budget_seconds:
                print(f'Time budget reached; pausing at block {last_block}')
                caught_up = False
                break
        print(f'  caught_up={caught_up}')
        if caught_up:
            last_block = latest_num
            last_ts = latest_ts
        else:
            try:
                last_ts = get_block_ts(last_block - 1)
            except Exception:
                pass

    # ---- aggregate: unlocked VVV still in user wallets, by time since unlock ----
    bands = {'<24h': 0.0, '1-7d': 0.0, '7-30d': 0.0, '30-90d': 0.0, '>90d': 0.0}
    held_supply = 0.0
    total_unlocked = 0.0
    for a, rec in wallets.items():
        for uts, amt in rec['unlocked']:
            age_days = max(0.0, (latest_ts - uts) / 86400.0)
            if age_days < 1:
                bands['<24h'] += amt
            elif age_days < 7:
                bands['1-7d'] += amt
            elif age_days < 30:
                bands['7-30d'] += amt
            elif age_days < 90:
                bands['30-90d'] += amt
            else:
                bands['>90d'] += amt
            held_supply += amt
            total_unlocked += amt
        if rec['legacy'] > 0:
            bands['>90d'] += rec['legacy']
            held_supply += rec['legacy']
            total_unlocked += rec['legacy']

    total_sold = sum(rec['sold_amt'] for rec in wallets.values())
    avg_days_to_sale = (sum(rec['sold_age_sum'] for rec in wallets.values()) / total_sold) if total_sold else 0.0

    summary = {
        'as_of': datetime.datetime.utcfromtimestamp(latest_ts).strftime('%Y-%m-%d %H:%M UTC'),
        'latest_block': latest_num,
        'backfill_complete': caught_up,
        'unlocked_still_held': round(held_supply, 2),
        'total_unlocked_alltime': round(total_unlocked + total_sold, 2),
        'total_sold': round(total_sold, 2),
        'hold_bands_by_age_since_unlock': {k: round(v, 2) for k, v in bands.items()},
        'avg_days_unlock_to_sale': round(avg_days_to_sale, 1),
    }

    os.makedirs('data', exist_ok=True)
    if caught_up:
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(summary, f, indent=2)
    elif os.path.exists(out_path):
        print('Unlock backfill not complete; keeping previous unlock.json')
    else:
        placeholder = {
            'as_of': datetime.datetime.utcfromtimestamp(latest_ts).strftime('%Y-%m-%d %H:%M UTC'),
            'backfill_complete': False,
            'message': 'Unlocked-VVV analysis is indexing transfer history; the sell-pressure snapshot will appear once complete.',
        }
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(placeholder, f, indent=2)

    write_state(state_path, last_block, last_ts, wallets)

    print('Unlocked VVV sell pressure:')
    for k, v in summary['hold_bands_by_age_since_unlock'].items():
        print(f'  held {k}: {v:,.0f}')
    print(f'  avg days unlock->sale: {summary["avg_days_unlock_to_sale"]}')
    print(f'  unlocked still held: {summary["unlocked_still_held"]:,.0f}')
    print(f'  total sold all-time: {summary["total_sold"]:,.0f}')


if __name__ == '__main__':
    main()
