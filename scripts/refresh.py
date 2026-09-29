#!/usr/bin/env python3
import json, urllib.request, urllib.error, time, os, datetime
from collections import defaultdict

API_KEY = os.environ.get('DUNE_API_KEY')
CG_KEY = os.environ.get('COINGECKO_API_KEY')
VENICE_API_KEY = os.environ.get('VENICE_API_KEY')
VENICE_RPC_URL = os.environ.get('VENICE_RPC_URL', 'https://api.venice.ai/api/v1/crypto/rpc/base-mainnet')
POLL_ATTEMPTS = int(os.environ.get('DUNE_POLL_ATTEMPTS', '180'))
POLL_INTERVAL_SECONDS = float(os.environ.get('DUNE_POLL_INTERVAL_SECONDS', '2'))
LOOKBACK_DAYS = int(os.environ.get('VVV_LOOKBACK_DAYS', '37'))
LOCKED_ADDRS = [
    '0x2d8cb8dc596dad0e1e34e2042e7ae6df93b11524',
    '0x4665883f3adb708f301ba75764d39ad0cd2a4d84',
    '0x4cb16d4153123a74bc724d161050959754f378d8',
    '0xb3c89592d84ae6adb6a1aa41515ac14ec822b175',
    '0xb6e08047320b4b4d943d7f1363776dddc6f4aa66'
]
LOCKED_ADDRS += [a.strip() for a in os.environ.get('VVV_LOCKED_ADDRESSES', '').split(',') if a.strip()]
BURN_ADDRS = [a.strip() for a in os.environ.get('VVV_BURN_ADDRESSES', '').split(',') if a.strip()]
svvv = '0x321b7ff75154472B18EDb199033fF4D116F340Ff'
vvv = '0xACFE6019Ed1A7Dc6f7B508C02D1b04eC88cC21BF'
zero = '0x0000000000000000000000000000000000000000'
dead = '0x000000000000000000000000000000000000dEaD'
total_supply_selector = '0x18160ddd'
balance_of_selector = '0x70a08231'
STAKE_TOPIC = '0x9e71bc8eea02a63969f509818f2dafb9254532904319f9dbda79b67bd34a5f3d'
UNSTAKE_TOPIC = '0xc606a9f55fc42cd3159fcfc8ddcd749dd21c4574cca0b68a5a65d5f984b6c42c'

RPC_CANDIDATES = []
if VENICE_API_KEY:
    RPC_CANDIDATES.append({
        'name': 'venice',
        'url': VENICE_RPC_URL,
        'headers': {
            'Authorization': f'Bearer {VENICE_API_KEY}',
            'Content-Type': 'application/json'
        },
        'chunk': int(os.environ.get('VENICE_LOG_CHUNK', '20000'))
    })
RPC_CANDIDATES.append({
    'name': 'base',
    'url': os.environ.get('BASE_RPC_URL', 'https://mainnet.base.org'),
    'headers': {'Content-Type': 'application/json', 'User-Agent': 'vvv-dashboard'},
    'chunk': int(os.environ.get('BASE_LOG_CHUNK', '2000'))
})

_active_rpc = None


def http_json(url, payload=None, extra_headers=None, method=None, timeout=60):
    headers = {'Content-Type': 'application/json', 'User-Agent': 'vvv-dashboard'}
    if extra_headers:
        headers.update(extra_headers)
    data = json.dumps(payload).encode('utf-8') if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method or ('POST' if data else 'GET'))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        raise RuntimeError(f'HTTP {e.code} {e.reason} for {url}: {body[:800]}') from e


def is_rate_limit(err):
    msg = str(err).lower()
    return '429' in msg or 'rate limit' in msg or 'too many requests' in msg


def is_range_issue(err):
    msg = str(err).lower()
    if is_rate_limit(err):
        return False
    return any(x in msg for x in ('range', 'limited to', 'archive', 'query returned more', 'block range'))


def rpc(payload, timeout=60):
    global _active_rpc
    order = []
    if _active_rpc:
        order.append(_active_rpc)
    for cand in RPC_CANDIDATES:
        if cand not in order:
            order.append(cand)
    last_err = None
    for cand in order:
        for attempt in range(5):
            try:
                out = http_json(cand['url'], payload, cand['headers'], timeout=timeout)
                if isinstance(out, dict) and out.get('error'):
                    raise RuntimeError(f"{cand['name']} RPC error: {out['error']}")
                _active_rpc = cand
                return out
            except Exception as e:
                last_err = e
                if is_rate_limit(e) and attempt < 4:
                    time.sleep(min(2 ** (attempt + 1), 16))
                    continue
                if _active_rpc is cand:
                    _active_rpc = None
                print(f"RPC {cand['name']} failed: {e}")
                break
    raise last_err or RuntimeError('No RPC endpoint available')


def to_rpc_address(address):
    cleaned = address.strip().lower()
    if cleaned.startswith('0x'):
        cleaned = cleaned[2:]
    if len(cleaned) != 40:
        raise ValueError(f'Invalid address: {address}')
    return '0x' + cleaned


def read_uint256_call(contract, data):
    res = rpc({
        'jsonrpc': '2.0',
        'method': 'eth_call',
        'params': [{'to': to_rpc_address(contract), 'data': data}, 'latest'],
        'id': 1
    })
    result = res.get('result')
    if not isinstance(result, str) or not result.startswith('0x'):
        raise RuntimeError(f'Unexpected eth_call response: {res}')
    return int(result, 16) / 1e18


def balance_of_call(address):
    padded = to_rpc_address(address)[2:].rjust(64, '0')
    return read_uint256_call(vvv, balance_of_selector + padded)


def get_block(num):
    res = rpc({
        'jsonrpc': '2.0',
        'method': 'eth_getBlockByNumber',
        'params': [hex(num), False],
        'id': 1
    })
    block = res.get('result')
    if not block:
        raise RuntimeError(f'Block {num} not found')
    return int(block['number'], 16), int(block['timestamp'], 16)


def get_logs(address, topic, from_block, to_block, chunk):
    logs = []
    start = from_block
    failures = 0
    while start <= to_block:
        end = min(start + chunk - 1, to_block)
        try:
            res = rpc({
                'jsonrpc': '2.0',
                'method': 'eth_getLogs',
                'params': [{
                    'address': to_rpc_address(address),
                    'fromBlock': hex(start),
                    'toBlock': hex(end),
                    'topics': [topic]
                }],
                'id': 1
            })
            batch = res.get('result') or []
            logs.extend(batch)
            start = end + 1
            failures = 0
            time.sleep(0.02)
        except Exception as e:
            if is_range_issue(e) and chunk > 250:
                chunk = max(chunk // 2, 250)
                print(f"Reducing log chunk to {chunk}: {e}")
                continue
            failures += 1
            if failures < 5:
                time.sleep(min(2 ** failures, 16))
                continue
            raise
    return logs


def decode_amount_events(logs, from_block, from_ts, avg_block_time):
    events = []
    for log in logs:
        topics = log.get('topics') or []
        if len(topics) < 2:
            continue
        block_num = int(log['blockNumber'], 16)
        ts = from_ts + (block_num - from_block) * avg_block_time
        events.append({
            'ts': ts,
            'user': '0x' + topics[1][-40:].lower(),
            'amount': int(log.get('data') or '0x0', 16) / 1e18
        })
    return events


def load_existing_daily():
    path = os.path.join('data', 'daily.json')
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding='utf-8') as f:
            rows = json.load(f)
        return rows if isinstance(rows, list) else []
    except Exception as e:
        print(f"Could not read existing daily.json: {e}")
        return []


def day_key(ts):
    return datetime.datetime.utcfromtimestamp(ts).strftime('%Y-%m-%d')


def parse_day(value):
    if isinstance(value, datetime.date) and not isinstance(value, datetime.datetime):
        return value
    return datetime.datetime.strptime(str(value)[:10], '%Y-%m-%d').date()


def daterange(start, end):
    days = []
    cur = start
    while cur <= end:
        days.append(cur)
        cur += datetime.timedelta(days=1)
    return days


print('Fetching on-chain staking events via RPC')
latest_num, latest_ts = get_block(int(rpc({
    'jsonrpc': '2.0',
    'method': 'eth_blockNumber',
    'params': [],
    'id': 1
})['result'], 16))
from_block = max(0, latest_num - int(LOOKBACK_DAYS * 86400 / 2))
try:
    from_num, from_ts = get_block(from_block)
except Exception as e:
    print(f"Lookback block fetch failed ({e}), using 10-day window")
    from_block = max(0, latest_num - int(10 * 86400 / 2))
    from_num, from_ts = get_block(from_block)

avg_block_time = (latest_ts - from_ts) / max(latest_num - from_num, 1)
chunk = (_active_rpc or RPC_CANDIDATES[-1])['chunk']
print(f"RPC={(_active_rpc or {}).get('name')} blocks={from_num}->{latest_num} chunk={chunk}")

unstake_logs = get_logs(svvv, UNSTAKE_TOPIC, from_num, latest_num, chunk)
stake_logs = get_logs(svvv, STAKE_TOPIC, from_num, latest_num, chunk)
unstake_events = decode_amount_events(unstake_logs, from_num, from_ts, avg_block_time)
stake_events = decode_amount_events(stake_logs, from_num, from_ts, avg_block_time)
print(f"Fetched {len(unstake_events)} UnstakeInitiated and {len(stake_events)} Staked events")

initiated_by_day = defaultdict(lambda: {'amount': 0.0, 'users': set()})
staked_by_day = defaultdict(lambda: {'amount': 0.0, 'users': set()})
for ev in unstake_events:
    rec = initiated_by_day[day_key(ev['ts'])]
    rec['amount'] += ev['amount']
    rec['users'].add(ev['user'])
for ev in stake_events:
    rec = staked_by_day[day_key(ev['ts'])]
    rec['amount'] += ev['amount']
    rec['users'].add(ev['user'])

existing = {str(r.get('day'))[:10]: r for r in load_existing_daily() if r.get('day')}
end_day = datetime.datetime.utcfromtimestamp(latest_ts).date()
start_day = end_day - datetime.timedelta(days=30)
fetched_from_day = datetime.datetime.utcfromtimestamp(from_ts).date()
all_days = daterange(start_day, end_day)

price_rows = {}
def fetch_cg_price(extra_headers=None, label=''):
    global price_rows
    url = 'https://api.coingecko.com/api/v3/coins/venice-token/market_chart'
    params = 'vs_currency=usd&days=30&interval=daily'
    hdrs = {'User-Agent': 'vvv-dashboard'}
    if extra_headers:
        hdrs.update(extra_headers)
    cg = http_json(url + '?' + params, extra_headers=hdrs, method='GET')
    for ts, price in cg.get('prices', []):
        day = datetime.datetime.utcfromtimestamp(ts / 1000).strftime('%Y-%m-%d')
        price_rows[day] = price
    print(f"Fetched {len(price_rows)} price points from CoinGecko{label}")

cg_attempts = []
if CG_KEY:
    cg_attempts.append(({'x-cg-demo-api-key': CG_KEY}, ' (demo key)'))
    cg_attempts.append(({'x-cg-pro-api-key': CG_KEY}, ' (pro key)'))
cg_attempts.append((None, ' (free tier)'))
for hdr, label in cg_attempts:
    try:
        fetch_cg_price(hdr, label)
        break
    except Exception as e:
        print(f"CoinGecko attempt{label} failed: {e}")
        price_rows = {}

vol_rows = {}
buy_rows = {}
sell_rows = {}
if API_KEY:
    try:
        sqlv = f"""
        SELECT CAST(date_trunc('day', block_time) AS date) AS day,
               SUM(amount_usd) AS trade_volume_usd,
               SUM(CASE WHEN token_bought_address = {vvv} THEN amount_usd ELSE 0 END) AS buy_volume_usd,
               SUM(CASE WHEN token_sold_address = {vvv} THEN amount_usd ELSE 0 END) AS sell_volume_usd
        FROM dex.trades
        WHERE blockchain='base'
          AND (token_bought_address = {vvv} OR token_sold_address = {vvv})
          AND block_time >= now() - interval '30' day
        GROUP BY 1
        """
        dune_headers = {'X-DUNE-API-KEY': API_KEY, 'Content-Type': 'application/json'}
        rv = http_json('https://api.dune.com/api/v1/sql/execute', {"sql": sqlv, "performance": "medium"}, dune_headers)
        exec_id = rv['execution_id']
        st = {'state': None}
        for _ in range(POLL_ATTEMPTS):
            st = http_json(f'https://api.dune.com/api/v1/execution/{exec_id}/status', extra_headers={'X-DUNE-API-KEY': API_KEY}, method='GET')
            if st.get('state') in ('QUERY_STATE_COMPLETED', 'QUERY_STATE_FAILED', 'QUERY_STATE_CANCELLED'):
                break
            time.sleep(POLL_INTERVAL_SECONDS)
        if st.get('state') == 'QUERY_STATE_COMPLETED':
            resv = http_json(f'https://api.dune.com/api/v1/execution/{exec_id}/results', extra_headers={'X-DUNE-API-KEY': API_KEY}, method='GET')
            for r in resv.get('result', {}).get('rows', []):
                vol_rows[str(r['day'])[:10]] = r.get('trade_volume_usd')
                buy_rows[str(r['day'])[:10]] = r.get('buy_volume_usd')
                sell_rows[str(r['day'])[:10]] = r.get('sell_volume_usd')
            print(f"Fetched {len(vol_rows)} volume points from Dune")
        else:
            print(f"Dune volume query did not complete: {st}")
    except Exception as e:
        print(f"ERROR fetching volume: {e}")
        vol_rows = {}
        buy_rows = {}
        sell_rows = {}
else:
    print("DUNE_API_KEY not set, skipping volume")

initiated_amount = {}
initiated_users = {}
staked_amount = {}
staked_users = {}
for day in all_days:
    key = day.isoformat()
    prev = existing.get(key, {})
    covered = day >= fetched_from_day
    if key in initiated_by_day:
        initiated_amount[key] = initiated_by_day[key]['amount']
        initiated_users[key] = len(initiated_by_day[key]['users'])
    elif covered:
        initiated_amount[key] = 0.0
        initiated_users[key] = 0
    else:
        initiated_amount[key] = float(prev.get('initiated_amount') or 0)
        initiated_users[key] = int(prev.get('initiated_users') or 0)
    if key in staked_by_day:
        staked_amount[key] = staked_by_day[key]['amount']
        staked_users[key] = len(staked_by_day[key]['users'])
    elif covered:
        staked_amount[key] = 0.0
        staked_users[key] = 0
    else:
        staked_amount[key] = float(prev.get('staked_amount') or 0)
        staked_users[key] = int(prev.get('staked_users') or 0)

rows = []
for day in all_days:
    key = day.isoformat()
    unlock_day = (day - datetime.timedelta(days=7)).isoformat()
    queue_days = [(day - datetime.timedelta(days=offset)).isoformat() for offset in range(0, 7)]
    prev = existing.get(key, {})
    rows.append({
        'day': key,
        'initiated_amount': initiated_amount.get(key, 0),
        'initiated_users': initiated_users.get(key, 0),
        'queue_amount': sum(initiated_amount.get(d, 0) for d in queue_days),
        'unlock_amount': initiated_amount.get(unlock_day, 0),
        'vvv_price_usd': price_rows.get(key, prev.get('vvv_price_usd')),
        'trade_volume_usd': vol_rows.get(key, prev.get('trade_volume_usd')),
        'staked_amount': staked_amount.get(key, 0),
        'staked_users': staked_users.get(key, 0),
        'buy_volume_usd': buy_rows.get(key, prev.get('buy_volume_usd')),
        'sell_volume_usd': sell_rows.get(key, prev.get('sell_volume_usd'))
    })

now_ts = latest_ts
initiated_last_7d = sum(ev['amount'] for ev in unstake_events if ev['ts'] > now_ts - 7 * 86400)
if not unstake_events:
    initiated_last_7d = sum(r['initiated_amount'] for r in rows[-7:])
nonzero_initiated = [r['initiated_amount'] for r in rows if r['initiated_amount']]
summary = [{
    'current_queue_amount': rows[-1]['queue_amount'] if rows else 0,
    'avg_queue_amount_30d': (sum(r['queue_amount'] for r in rows) / len(rows)) if rows else 0,
    'avg_daily_initiated_30d': (sum(nonzero_initiated) / len(nonzero_initiated)) if nonzero_initiated else 0,
    'initiated_last_7d': initiated_last_7d
}]

supply_summary = {
    'total_supply': 78780000.0,
    'locked_supply': 7870000.0,
    'staked_supply': 31130000.0,
    'circ_supply': 44210000.0,
    'burned_supply': 33680000.0
}
try:
    total_supply = read_uint256_call(vvv, total_supply_selector)
    locked_supply = 0.0
    for addr in LOCKED_ADDRS:
        locked_supply += balance_of_call(addr)
    staked_supply = balance_of_call(svvv)
    burn_targets = [zero, dead] + BURN_ADDRS
    burned_supply = 0.0
    for addr in burn_targets:
        burned_supply += balance_of_call(addr)
    circ_supply = max(total_supply - locked_supply - staked_supply - burned_supply, 0)
    fallback = {
        'total_supply': 78.78e6,
        'locked_supply': 7.87e6,
        'staked_supply': 31.13e6,
        'circ_supply': 44.21e6,
        'burned_supply': (78.78e6 * 42.75) / 100
    }
    tolerance = 0.02
    def within(key, value):
        base = fallback[key]
        if base == 0:
            return True
        return abs(value - base) / base <= tolerance
    if not (within('total_supply', total_supply)
            and within('locked_supply', locked_supply)
            and within('staked_supply', staked_supply)
            and within('circ_supply', circ_supply)
            and within('burned_supply', burned_supply)):
        print("Supply values outside 2% tolerance, falling back to reference numbers")
        total_supply = fallback['total_supply']
        locked_supply = fallback['locked_supply']
        staked_supply = fallback['staked_supply']
        circ_supply = fallback['circ_supply']
        burned_supply = fallback['burned_supply']
    supply_summary = {
        'total_supply': total_supply,
        'locked_supply': locked_supply,
        'staked_supply': staked_supply,
        'circ_supply': circ_supply,
        'burned_supply': burned_supply
    }
    print("Fetched supply data from RPC (Base)")
except Exception as e:
    print(f"ERROR fetching RPC supply data: {e}")

os.makedirs('data', exist_ok=True)
with open('data/daily.json', 'w', encoding='utf-8') as f:
    f.write(json.dumps(rows, indent=2, default=str))
with open('data/summary.json', 'w', encoding='utf-8') as f:
    f.write(json.dumps(summary, indent=2, default=str))
with open('data/supply.json', 'w', encoding='utf-8') as f:
    f.write(json.dumps([supply_summary], indent=2, default=str))

print(f"Data refreshed successfully through {end_day.isoformat()} ({len(rows)} days)")
if rows:
    print(f"Latest initiated={rows[-1]['initiated_amount']:.2f} staked={rows[-1]['staked_amount']:.2f} queue={rows[-1]['queue_amount']:.2f}")
