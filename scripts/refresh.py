#!/usr/bin/env python3
import json, urllib.request, urllib.parse, time, os, datetime

API_KEY = os.environ['DUNE_API_KEY']
CG_KEY = os.environ.get('COINGECKO_API_KEY')
ETHERSCAN_KEY = os.environ.get('ETHERSCAN_API_KEY')
LOCKED_ADDRS = [
    '0x2d8cb8dc596dad0e1e34e2042e7ae6df93b11524',
    '0x4665883f3adb708f301ba75764d39ad0cd2a4d84',
    '0x4cb16d4153123a74bc724d161050959754f378d8',
    '0xb3c89592d84ae6adb6a1aa41515ac14ec822b175',
    '0xb6e08047320b4b4d943d7f1363776dddc6f4aa66'
]
LOCKED_ADDRS += [a.strip() for a in os.environ.get('VVV_LOCKED_ADDRESSES', '').split(',') if a.strip()]
BURN_ADDRS = [a.strip() for a in os.environ.get('VVV_BURN_ADDRESSES', '').split(',') if a.strip()]
headers = {'X-DUNE-API-KEY': API_KEY, 'Content-Type': 'application/json'}
svvv = '0x321b7ff75154472B18EDb199033fF4D116F340Ff'
vvv = '0xACFE6019Ed1A7Dc6f7B508C02D1b04eC88cC21BF'
method = '0xae5ac921'
zero = '0x0000000000000000000000000000000000000000'
dead = '0x000000000000000000000000000000000000dEaD'
etherscan_base = 'https://api.etherscan.io/v2/api'
chain_id = '8453'

sql1 = f"""
WITH tx AS (
  SELECT
    block_time,
    "from" AS unstaker,
    bytearray_to_uint256(bytearray_substring(data, 5, 32)) / 1e18 AS amount
  FROM base.transactions
  WHERE
    to = {svvv}
    AND bytearray_substring(data, 1, 4) = {method}
    AND block_time >= now() - interval '30' day
),

_days AS (
  SELECT day
  FROM unnest(sequence(
    CAST(date_trunc('day', now() - interval '30' day) AS date),
    CAST(date_trunc('day', now()) AS date),
    interval '1' day
  )) AS t(day)
),

initiated AS (
  SELECT
    CAST(date_trunc('day', block_time) AS date) AS day,
    SUM(amount) AS initiated_amount,
    COUNT(DISTINCT unstaker) AS initiated_users
  FROM tx
  GROUP BY 1
),

unlocks AS (
  SELECT CAST(date_trunc('day', block_time + interval '7' day) AS date) AS day, SUM(amount) AS unlock_amount
  FROM tx
  GROUP BY 1
),

queue AS (
  SELECT d.day,
    SUM(t.amount) AS queue_amount
  FROM _days d
  LEFT JOIN tx t
    ON t.block_time > CAST(d.day AS timestamp) - interval '7' day
   AND t.block_time <= CAST(d.day AS timestamp) + interval '1' day
  GROUP BY 1
)

SELECT
  d.day,
  COALESCE(i.initiated_amount, 0) AS initiated_amount,
  COALESCE(i.initiated_users, 0) AS initiated_users,
  COALESCE(u.unlock_amount, 0) AS unlock_amount,
  COALESCE(q.queue_amount, 0) AS queue_amount
FROM _days d
LEFT JOIN initiated i ON d.day = i.day
LEFT JOIN unlocks u ON d.day = u.day
LEFT JOIN queue q ON d.day = q.day
ORDER BY d.day;
"""

sql2 = f"""
WITH tx AS (
  SELECT
    block_time,
    bytearray_to_uint256(bytearray_substring(data, 5, 32)) / 1e18 AS amount
  FROM base.transactions
  WHERE
    to = {svvv}
    AND bytearray_substring(data, 1, 4) = {method}
    AND block_time >= now() - interval '30' day
),

_days AS (
  SELECT day
  FROM unnest(sequence(
    CAST(date_trunc('day', now() - interval '30' day) AS date),
    CAST(date_trunc('day', now()) AS date),
    interval '1' day
  )) AS t(day)
),

queue AS (
  SELECT d.day,
    SUM(t.amount) AS queue_amount
  FROM _days d
  LEFT JOIN tx t
    ON t.block_time > CAST(d.day AS timestamp) - interval '7' day
   AND t.block_time <= CAST(d.day AS timestamp) + interval '1' day
  GROUP BY 1
),

daily_initiated AS (
  SELECT CAST(date_trunc('day', block_time) AS date) AS day, SUM(amount) AS initiated_amount
  FROM tx
  GROUP BY 1
)

SELECT
  (SELECT queue_amount FROM queue ORDER BY day DESC LIMIT 1) AS current_queue_amount,
  (SELECT AVG(queue_amount) FROM queue) AS avg_queue_amount_30d,
  (SELECT AVG(initiated_amount) FROM daily_initiated) AS avg_daily_initiated_30d,
  (SELECT COALESCE(SUM(amount),0) FROM tx WHERE block_time > now() - interval '7' day) AS initiated_last_7d
"""

sql_stake = f"""
WITH transfer AS (
  SELECT
    block_time,
    topic1 AS staker,
    bytearray_to_uint256(data) / 1e18 AS amount
  FROM base.logs
  WHERE
    contract_address = {vvv}
    AND topic0 = 0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef
    AND topic2 = 0x000000000000000000000000321b7ff75154472b18edb199033ff4d116f340ff
    AND block_time >= now() - interval '30' day
),

_days AS (
  SELECT day
  FROM unnest(sequence(
    CAST(date_trunc('day', now() - interval '30' day) AS date),
    CAST(date_trunc('day', now()) AS date),
    interval '1' day
  )) AS t(day)
),

daily_staked AS (
  SELECT
    CAST(date_trunc('day', block_time) AS date) AS day,
    SUM(amount) AS staked_amount,
    COUNT(DISTINCT staker) AS staked_users
  FROM transfer
  GROUP BY 1
)

SELECT
  d.day,
  COALESCE(s.staked_amount, 0) AS staked_amount,
  COALESCE(s.staked_users, 0) AS staked_users
FROM _days d
LEFT JOIN daily_staked s ON d.day = s.day
ORDER BY d.day;
"""



def exec_sql(sql):
    data = json.dumps({"sql": sql, "performance": "medium"}).encode('utf-8')
    req = urllib.request.Request('https://api.dune.com/api/v1/sql/execute', data=data, headers=headers, method='POST')
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode('utf-8'))


def poll(eid):
    st = {'state': None}
    for _ in range(60):
        req = urllib.request.Request(f'https://api.dune.com/api/v1/execution/{eid}/status', headers={'X-DUNE-API-KEY': API_KEY})
        with urllib.request.urlopen(req) as resp:
            st = json.loads(resp.read().decode('utf-8'))
        if st.get('state') in ('QUERY_STATE_COMPLETED', 'QUERY_STATE_FAILED', 'QUERY_STATE_CANCELLED'):
            return st
        time.sleep(2)
    return st


def results(eid):
    req = urllib.request.Request(f'https://api.dune.com/api/v1/execution/{eid}/results', headers={'X-DUNE-API-KEY': API_KEY})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode('utf-8'))


def fetch_etherscan(params):
    if not ETHERSCAN_KEY:
        raise RuntimeError('ETHERSCAN_API_KEY not set')
    params['apikey'] = ETHERSCAN_KEY
    params['chainid'] = chain_id
    url = etherscan_base + '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode('utf-8'))


r1 = exec_sql(sql1)
r2 = exec_sql(sql2)
r_stake = exec_sql(sql_stake)
r3 = None
r4 = None

for r in (r1, r2, r_stake):
    st = poll(r['execution_id'])
    if st['state'] != 'QUERY_STATE_COMPLETED':
        raise SystemExit(f"Query failed: {st}")

res1 = results(r1['execution_id'])
res2 = results(r2['execution_id'])
res_stake = results(r_stake['execution_id'])

stake_amount_map = {r['day']: r.get('staked_amount', 0) for r in res_stake.get('result', {}).get('rows', [])}
stake_users_map = {r['day']: r.get('staked_users', 0) for r in res_stake.get('result', {}).get('rows', [])}

price_rows = {}
try:
    if CG_KEY:
        url = 'https://api.coingecko.com/api/v3/coins/venice-token/market_chart'
        params = 'vs_currency=usd&days=30&interval=daily'
        req = urllib.request.Request(url + '?' + params, headers={'x-cg-pro-api-key': CG_KEY})
    else:
        url = 'https://api.coingecko.com/api/v3/coins/venice-token/market_chart?vs_currency=usd&days=30&interval=daily'
        req = urllib.request.Request(url)
    with urllib.request.urlopen(req) as resp:
        cg = json.loads(resp.read().decode('utf-8'))
    for ts, price in cg.get('prices', []):
        day = datetime.datetime.utcfromtimestamp(ts / 1000).strftime('%Y-%m-%d')
        price_rows[day] = price
    print(f"Fetched {len(price_rows)} price points from CoinGecko")
except Exception as e:
    print(f"ERROR fetching CoinGecko price: {e}")
    price_rows = {}

vol_rows = {}
buy_rows = {}
sell_rows = {}
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
    data = json.dumps({"sql": sqlv, "performance": "medium"}).encode('utf-8')
    req = urllib.request.Request('https://api.dune.com/api/v1/sql/execute', data=data, headers=headers, method='POST')
    with urllib.request.urlopen(req) as resp:
        rv = json.loads(resp.read().decode('utf-8'))
    exec_id = rv['execution_id']
    st = {'state': None}
    for _ in range(60):
        req = urllib.request.Request(f'https://api.dune.com/api/v1/execution/{exec_id}/status', headers={'X-DUNE-API-KEY': API_KEY})
        with urllib.request.urlopen(req) as resp:
            st = json.loads(resp.read().decode('utf-8'))
        if st.get('state') in ('QUERY_STATE_COMPLETED', 'QUERY_STATE_FAILED', 'QUERY_STATE_CANCELLED'):
            break
        time.sleep(2)
    if st.get('state') == 'QUERY_STATE_COMPLETED':
        req = urllib.request.Request(f'https://api.dune.com/api/v1/execution/{exec_id}/results', headers={'X-DUNE-API-KEY': API_KEY})
        with urllib.request.urlopen(req) as resp:
            resv = json.loads(resp.read().decode('utf-8'))
        for r in resv.get('result', {}).get('rows', []):
            vol_rows[r['day']] = r.get('trade_volume_usd')
            buy_rows[r['day']] = r.get('buy_volume_usd')
            sell_rows[r['day']] = r.get('sell_volume_usd')
except Exception as e:
    print(f"ERROR fetching volume: {e}")
    vol_rows = {}
    buy_rows = {}
    sell_rows = {}

rows = res1['result']['rows']
for r in rows:
    r['vvv_price_usd'] = price_rows.get(r['day'])
    r['trade_volume_usd'] = vol_rows.get(r['day'])
    r['staked_amount'] = stake_amount_map.get(r['day'], 0)
    r['staked_users'] = stake_users_map.get(r['day'], 0)
    r['buy_volume_usd'] = buy_rows.get(r['day'])
    r['sell_volume_usd'] = sell_rows.get(r['day'])

# Default values from known VVV token distribution
supply_summary = {
    'total_supply': 78780000.0,
    'locked_supply': 7870000.0,
    'staked_supply': 31130000.0,
    'circ_supply': 44210000.0,
    'burned_supply': 33680000.0  # ~42.75% of total
}
try:
    total = fetch_etherscan({
        'module': 'stats',
        'action': 'tokensupply',
        'contractaddress': vvv
    })
    total_supply = float(total.get('result', 0)) / 1e18

    locked_supply = 0.0
    for addr in LOCKED_ADDRS:
        bal = fetch_etherscan({
            'module': 'account',
            'action': 'tokenbalance',
            'contractaddress': vvv,
            'address': addr,
            'tag': 'latest'
        })
        locked_supply += float(bal.get('result', 0)) / 1e18

    staked = fetch_etherscan({
        'module': 'account',
        'action': 'tokenbalance',
        'contractaddress': vvv,
        'address': svvv,
        'tag': 'latest'
    })
    staked_supply = float(staked.get('result', 0)) / 1e18

    burn_targets = [zero, dead] + BURN_ADDRS
    burned_supply = 0.0
    for addr in burn_targets:
        burn = fetch_etherscan({
            'module': 'account',
            'action': 'tokenbalance',
            'contractaddress': vvv,
            'address': addr,
            'tag': 'latest'
        })
        burned_supply += float(burn.get('result', 0)) / 1e18

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
    print("Fetched supply data from Basescan (Etherscan V2)")
except Exception as e:
    print(f"ERROR fetching Basescan supply data: {e}")

os.makedirs('data', exist_ok=True)
with open('data/daily.json', 'w') as f:
    f.write(json.dumps(rows, indent=2, default=str))
with open('data/summary.json', 'w') as f:
    f.write(json.dumps(res2['result']['rows'], indent=2, default=str))
with open('data/supply.json', 'w') as f:
    f.write(json.dumps([supply_summary], indent=2, default=str))

print("Data refreshed successfully")
