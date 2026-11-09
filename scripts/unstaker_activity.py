#!/usr/bin/env python3
"""Top seven initiators (rolling 7d), and their verified post-claim activity (90d).

Independent, resumable indexer: never imports refresh.py (which runs on import).
Amounts stay in wei until publication. FIFO is an attribution convention, not
proof of which fungible tokens were sold. No wallet-ownership labels are inferred.
"""
import bisect
import datetime as dt
import json
import os
import time
import urllib.request
from collections import defaultdict, deque
from pathlib import Path

VVV = '0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf'
STAKING = '0x321b7ff75154472b18edb199033ff4d116f340ff'
TRANSFER = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
INITIATED = '0xc606a9f55fc42cd3159fcfc8ddcd749dd21c4574cca0b68a5a65d5f984b6c42c'
FINALIZED = '0xc819022d26707b1e41f61d9adc9481024e27272d949e34a1dfec4d7abf168a33'
# finalizeUnstake(), finalizeUnstake(address), finalizeUnstake(uint256).
FINALIZE_METHODS = {'0x0ea94341', '0x823b3b64', '0x40d1ee22'}
SWAP_V2 = '0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822'
SWAP_V3 = '0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67'
SWAP_SLIPSTREAM = '0x19b47279256b2a23a1665c810c8d55a1758940ee09377d4f8d26497a3577dc83'
BUCKETS = ['Within 24h', '24–48h', '48–72h', '3–7 days', 'After 7 days', 'Still holding']
KINDS = ['sold', 'transferred', 'other_transfer', 'restaked', 'held']
DAY = 86400
UNIT = 10 ** 18
DATA = Path(__file__).resolve().parents[1] / 'data'


class BudgetExpired(RuntimeError):
    pass


class IndexUnavailable(RuntimeError):
    pass


def number(value):
    return int(value, 16) if isinstance(value, str) else int(value)


def topic_address(address):
    return '0x' + address.lower().removeprefix('0x').rjust(64, '0')


def utc(timestamp):
    return dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).isoformat().replace('+00:00', 'Z')


def atomic_json(path, data):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def load_json(path, fallback):
    if not path.exists():
        return fallback
    return json.loads(path.read_text(encoding='utf-8'))


def words(data, signed=False):
    raw = data.removeprefix('0x')
    if len(raw) % 64:
        raise ValueError('Malformed ABI words')
    result = [int(raw[i:i + 64], 16) for i in range(0, len(raw), 64)]
    if signed:
        return [value - 2 ** 256 if value >= 2 ** 255 else value for value in result]
    return result


def normalize(log):
    return {'block': number(log['blockNumber']), 'index': number(log['logIndex']),
            'tx': log['transactionHash'].lower(), 'address': log['address'].lower(),
            'topics': [topic.lower() for topic in log['topics']], 'data': log['data']}


def transfers(logs):
    result = []
    for log in logs:
        topics = log['topics']
        if len(topics) == 3 and topics[0] == TRANSFER:
            result.append({**log, 'from': '0x' + topics[1][-40:],
                           'to': '0x' + topics[2][-40:], 'amount': number(log['data'])})
    return result


class RPC:
    def __init__(self, budget):
        self.deadline = time.monotonic() + budget
        self.blocks = {}
        self.receipts = {}
        self.transactions = {}
        self.tokens = {}
        self.codes = {}
        self.last_request = 0
        self.chunk = min(40000, int(os.environ.get('VVV_ACTIVITY_LOG_CHUNK', '10000')))
        self.providers = []
        self.index_provider = None
        self.has_transfer_index = False
        key = os.environ.get('VENICE_API_KEY')
        if key:
            self.providers.append((os.environ.get('VENICE_RPC_URL', 'https://api.venice.ai/api/v1/crypto/rpc/base-mainnet'),
                                   {'Authorization': 'Bearer ' + key}))
        alchemy_key = os.environ.get('ALCHEMY_API_KEY')
        if alchemy_key:
            self.index_provider = (f'https://base-mainnet.g.alchemy.com/v2/{alchemy_key}', {})
            self.providers.append(self.index_provider)
            self.has_transfer_index = os.environ.get('VVV_ACTIVITY_HISTORY_SOURCE', 'auto') != 'rpc'
        self.providers.extend((url, {}) for url in dict.fromkeys([
            os.environ.get('PUBLICNODE_RPC_URL', 'https://base-rpc.publicnode.com'),
            os.environ.get('BASE_RPC_URL', 'https://mainnet.base.org')]))

    def check(self):
        if time.monotonic() >= self.deadline:
            raise BudgetExpired('Activity indexing time budget reached; resuming next refresh')

    def call(self, method, params):
        self.check()
        last_error = None
        providers = [self.index_provider] if method == 'alchemy_getAssetTransfers' and self.index_provider else self.providers
        for url, headers in providers:
            for attempt in range(3):
                self.check()
                time.sleep(max(0, 0.65 - (time.monotonic() - self.last_request)))
                request = urllib.request.Request(url, data=json.dumps({
                    'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}).encode(),
                    headers={'Content-Type': 'application/json', 'User-Agent': 'vvv-wallet-activity', **headers})
                try:
                    self.last_request = time.monotonic()
                    with urllib.request.urlopen(request, timeout=min(30, max(1, self.deadline - time.monotonic()))) as response:
                        output = json.load(response)
                    if output.get('error') or output.get('result') is None:
                        raise RuntimeError(str(output.get('error', 'RPC returned null')))
                    return output['result']
                except Exception as exc:
                    last_error = exc
                    # Provider errors must never expose credentials embedded in URLs.
                    message = str(exc).replace(os.environ.get('ALCHEMY_API_KEY') or '\0', '[redacted]').lower()
                    last_error = message
                    if any(s in message for s in ('range', 'limited to', 'too many results')):
                        raise
                    if '403' in message or '402' in message:
                        break
                    self.check()
                    time.sleep(2 ** attempt)
        raise RuntimeError(f'{method} failed: {last_error}')

    def asset_transfers(self, address, start, end, direction, page_key=None):
        if not self.index_provider:
            raise IndexUnavailable('No authenticated transfer index is configured')
        options = {'fromBlock': hex(start), 'toBlock': hex(end),
                   'contractAddresses': [VVV], 'category': ['erc20'],
                   'excludeZeroValue': True, 'maxCount': '0x3e8', 'order': 'asc',
                   'fromAddress' if direction == 'outgoing' else 'toAddress': address}
        if page_key:
            options['pageKey'] = page_key
        try:
            result = self.call('alchemy_getAssetTransfers', [options])
        except BudgetExpired:
            raise
        except RuntimeError as exc:
            raise IndexUnavailable(str(exc)) from exc
        if not isinstance(result, dict) or not isinstance(result.get('transfers'), list):
            raise IndexUnavailable('Transfer index returned an unsupported response')
        return result

    def timestamp(self, block):
        key = str(block)
        if key not in self.blocks:
            result = self.call('eth_getBlockByNumber', [hex(block), False])
            if number(result['number']) != block:
                raise RuntimeError('RPC returned a different block')
            self.blocks[key] = number(result['timestamp'])
        return self.blocks[key]

    def first_block(self, timestamp, latest):
        low, high = 0, latest
        while low < high:
            middle = (low + high) // 2
            if self.timestamp(middle) < timestamp:
                low = middle + 1
            else:
                high = middle
        return low

    def logs(self, address, topics, start, end):
        result = self.call('eth_getLogs', [{'address': address, 'topics': topics,
                           'fromBlock': hex(start), 'toBlock': hex(end)}])
        if not isinstance(result, list):
            raise RuntimeError('eth_getLogs did not return an array')
        if len(result) >= 9000:
            raise RuntimeError('Log range hit result limit')
        for log in result:
            if log.get('removed') or not start <= number(log['blockNumber']) <= end:
                raise RuntimeError('Unstable or out-of-range log response')
            if log['address'].lower() != address.lower():
                raise RuntimeError('RPC returned logs for a different contract')
            for index, expected in enumerate(topics):
                if expected is not None and (len(log['topics']) <= index or log['topics'][index].lower() != expected.lower()):
                    raise RuntimeError('RPC returned logs outside the topic filter')
        return [normalize(log) for log in result]

    def receipt(self, tx):
        if tx not in self.receipts:
            result = self.call('eth_getTransactionReceipt', [tx])
            if result['transactionHash'].lower() != tx or number(result.get('status', '0x0')) != 1:
                raise RuntimeError('Unsuccessful/mismatched receipt')
            self.receipts[tx] = [normalize(log) for log in result['logs']]
        return self.receipts[tx]

    def transaction(self, tx):
        if tx not in self.transactions:
            self.transactions[tx] = self.call('eth_getTransactionByHash', [tx])
        return self.transactions[tx]

    def pool_tokens(self, pool, block):
        key = f'{pool}:{block}'
        if key not in self.tokens:
            self.tokens[key] = tuple('0x' + self.call('eth_call', [
                {'to': pool, 'data': selector}, hex(block)])[-40:].lower()
                for selector in ('0x0dfe1681', '0xd21220a7'))
        return self.tokens[key]

    def is_contract(self, address, block):
        key = f'{address}:{block}'
        if key not in self.codes:
            self.codes[key] = self.call('eth_getCode', [address, hex(block)]) not in ('0x', '0x0')
        return self.codes[key]


def verified_claim_amount(wallet, tx, logs, transaction):
    """Require explicit finalization evidence; staking rewards alone aren't claims."""
    released = sum(t['amount'] for t in transfers(logs)
                   if t['address'] == VVV and t['from'] == STAKING and t['to'] == wallet)
    if not released:
        return 0
    finalized = 0
    for log in logs:
        if log['address'] == STAKING and len(log['topics']) == 2 and log['topics'][0] == FINALIZED:
            if '0x' + log['topics'][1][-40:] == wallet:
                values = words(log['data'])
                if len(values) == 1:
                    finalized += values[0]
    if finalized:
        return min(released, finalized)
    # Direct verified finalization calls also work if the staking implementation
    # doesn't emit the known finalization event. Routed/unknown calls stay unverified.
    if ((transaction.get('to') or '').lower() == STAKING
            and transaction.get('from', '').lower() == wallet
            and transaction.get('input', '')[:10].lower() in FINALIZE_METHODS):
        return released
    return 0


def verified_swap_amount(wallet, logs, transaction, pool_tokens):
    """Conservative V2/V3/Slipstream swap evidence, never a pool transfer alone.

    Require wallet-signed tx, a VVV input path to a pool emitting Swap, pool token
    verification, and a net non-VVV ERC20 output to the same wallet. Native-ETH,
    V4 and unsupported swaps remain unverified transfers rather than guessed sales.
    """
    if transaction.get('from', '').lower() != wallet:
        return 0
    moves = transfers(logs)
    vvv_moves = [t for t in moves if t['address'] == VVV]
    net_out = (sum(t['amount'] for t in vvv_moves if t['from'] == wallet)
               - sum(t['amount'] for t in vvv_moves if t['to'] == wallet))
    if net_out <= 0:
        return 0
    # Carry only this wallet's VVV through routers; unrelated swaps in the same
    # receipt cannot supply the pool's verified input allocation.
    flow = defaultdict(int, {wallet: net_out})
    pool_inputs = defaultdict(int)
    for transfer in vvv_moves:
        carried = min(flow[transfer['from']], transfer['amount'])
        flow[transfer['from']] -= carried
        flow[transfer['to']] += carried
        pool_inputs[transfer['to']] += carried
    sold = 0
    for log in logs:
        signature = log['topics'][0] if log['topics'] else None
        if signature not in (SWAP_V2, SWAP_V3, SWAP_SLIPSTREAM) or not pool_inputs[log['address']]:
            continue
        token0, token1 = pool_tokens(log['address'], log['block'])
        if VVV not in (token0, token1) or token0 == token1:
            continue
        index = 0 if token0 == VVV else 1
        quote = token1 if index == 0 else token0
        quote_received = (sum(t['amount'] for t in moves if t['address'] == quote and t['to'] == wallet)
                          - sum(t['amount'] for t in moves if t['address'] == quote and t['from'] == wallet))
        if quote_received <= 0:
            continue
        values = words(log['data'], signed=signature != SWAP_V2)
        if signature == SWAP_V2:
            if len(values) != 4 or values[1 - index] != 0 or values[index + 2] != 0 or values[3 - index] <= 0:
                continue
            amount_in = values[index]
            amount_out = values[3 - index]
        else:
            if len(values) < 5 or values[index] <= 0 or values[1 - index] >= 0:
                continue
            amount_in = values[index]
            amount_out = -values[1 - index]
        quote_flow = defaultdict(int, {log['address']: amount_out})
        delivered = 0
        for move in moves:
            if move['address'] != quote:
                continue
            carried = min(quote_flow[move['from']], move['amount'])
            quote_flow[move['from']] -= carried
            quote_flow[move['to']] += carried
            if move['to'] == wallet:
                delivered += carried
        if not delivered:
            continue
        take = min(amount_in, pool_inputs[log['address']], max(0, net_out - sold))
        sold += take
        pool_inputs[log['address']] -= take
    return sold


def age_bucket(seconds):
    return bisect.bisect_right([DAY, 2 * DAY, 3 * DAY, 7 * DAY], max(0, seconds))


def rank_wallets(logs, timestamp, now, block_range=None):
    totals = defaultdict(lambda: {'amount': 0, 'count': 0})
    for log in logs:
        if (log['address'] != STAKING or len(log['topics']) != 2
                or log['topics'][0] != INITIATED or len(words(log['data'])) != 1):
            continue
        if block_range is not None:
            # The first block was binary-searched at exactly now-7d, and the
            # last block is the pinned snapshot. No per-event timestamp RPCs
            # are needed just to rank events inside those exact boundaries.
            if not block_range[0] <= log['block'] <= block_range[1]:
                continue
        elif not now - 7 * DAY <= timestamp(log['block']) <= now:
            continue
        address = '0x' + log['topics'][1][-40:]
        totals[address]['amount'] += number(log['data'])
        totals[address]['count'] += 1
    selected = sorted(totals, key=lambda address: (-totals[address]['amount'], address))[:7]
    return selected, totals


def analyze_wallet(wallet, logs, opening, claim_amounts, sale_amounts, contracts, timestamps, now, since):
    """Replay all VVV, including non-claim balances, so rewards aren't sold claims."""
    lots = deque([[None, opening, None]]) if opening else deque()
    buckets = {key: [0] * len(BUCKETS) for key in KINDS}
    cycles = []
    moves = transfers(sorted(logs, key=lambda log: (log['block'], log['index'])))
    claim_left = dict(claim_amounts)
    sale_left = dict(sale_amounts)
    # Refund/change in a swap belongs back to the input balance, not a new lot.
    refunds = {}
    for tx, sold in sale_amounts.items():
        if sold:
            refunds[tx] = sum(t['amount'] for t in moves if t['tx'] == tx and t['to'] == wallet
                              and t['from'] != STAKING)
    skip_refund = dict(refunds)
    unexplained_out = 0
    for transfer in moves:
        src, dst, tx = transfer['from'], transfer['to'], transfer['tx']
        amount = transfer['amount']
        timestamp = timestamps[str(transfer['block'])]
        if src == dst:
            continue
        if dst == wallet:
            if src != STAKING and skip_refund.get(tx, 0):
                skipped = min(amount, skip_refund[tx])
                skip_refund[tx] -= skipped
                amount -= skipped
            claimed = min(amount, claim_left.get(tx, 0)) if src == STAKING else 0
            if claimed:
                claim_left[tx] -= claimed
                cycle = {'claimed_at': utc(timestamp), 'tx': tx, 'claimed': claimed,
                         **{key: 0 for key in KINDS}}
                cycles.append(cycle)
                lots.append([timestamp, claimed, cycle])
            if amount > claimed:
                lots.append([None, amount - claimed, None])
        elif src == wallet:
            refund = min(amount, refunds.get(tx, 0))
            refunds[tx] = refunds.get(tx, 0) - refund
            remaining = amount - refund
            sold = min(remaining, sale_left.get(tx, 0))
            sale_left[tx] = sale_left.get(tx, 0) - sold
            categories = [(sold, 'sold'), (remaining - sold,
                'restaked' if dst == STAKING else 'other_transfer' if dst in contracts else 'transferred')]
            for quantity, kind in categories:
                while quantity and lots:
                    claimed_at, balance, cycle = lots[0]
                    take = min(quantity, balance)
                    if cycle and claimed_at >= since:
                        buckets[kind][age_bucket(timestamp - claimed_at)] += take
                        cycle[kind] += take
                    quantity -= take
                    lots[0][1] -= take
                    if not lots[0][1]:
                        lots.popleft()
                unexplained_out += quantity
    for claimed_at, balance, cycle in lots:
        if cycle and claimed_at >= since:
            buckets['held'][-1] += balance
            cycle['held'] += balance
    for cycle in cycles:
        for key in ['claimed'] + KINDS:
            cycle[key] = round(cycle[key] / UNIT, 8)
    return {'series': {key: [round(value / UNIT, 8) for value in values] for key, values in buckets.items()},
            'claims': cycles[-50:], 'claim_count': len(cycles),
            'observed_days': round((now - since) / DAY, 1),
            'balance_wei': sum(lot[1] for lot in lots),
            'unreconciled_outflow': round(unexplained_out / UNIT, 8)}


def scan(rpc, state, key, address, topics_list, start, latest, save):
    section = state.setdefault(key, {'next_block': start, 'logs': []})
    cursor = max(start, section['next_block'])
    section['logs'] = [log for log in section['logs'] if log['block'] >= start]
    chunks = 0
    while cursor <= latest:
        rpc.check()
        end = min(latest, cursor + rpc.chunk - 1)
        try:
            batches = [rpc.logs(address, topics, cursor, end) for topics in topics_list]
        except Exception as exc:
            if any(word in str(exc).lower() for word in ('range', 'limit', 'too many results')) and rpc.chunk > 250:
                rpc.chunk = max(250, rpc.chunk // 2)
                continue
            raise
        merged = {(log['tx'], log['index']): log for batch in batches for log in batch}
        section['logs'].extend(merged.values())
        section['next_block'] = end + 1
        save()
        cursor = end + 1
        chunks += 1
        if chunks % 25 == 0:
            print(f'{key}: indexed through block {end} of {latest}', flush=True)
    return section['logs']


def indexed_history(rpc, cache, address, start, latest, save):
    """Discover txs through a paginated index; replay only exact receipt logs.

    Freeze the range while paging (page keys depend on the original query).
    Checkpoint each receipt so an interrupted page does not repeat archive reads.
    The index's rounded amounts, human labels and timestamps are never used as
    claim/sale evidence. A balance reconciliation is still required downstream.
    """
    indexed = cache.setdefault('indexed_history', {'start': start, 'end': latest,
        'directions': {direction: {'page': None, 'done': False} for direction in ('outgoing', 'incoming')},
        'transactions': [], 'logs': []})
    processed = set(indexed['transactions'])
    for direction, progress in indexed['directions'].items():
        while not progress['done']:
            rpc.check()
            page = rpc.asset_transfers(address, indexed['start'], indexed['end'], direction, progress['page'])
            for entry in page['transfers']:
                rpc.check()
                tx = entry.get('hash', '').lower()
                block = number(entry['blockNum'])
                contract = (entry.get('rawContract') or {}).get('address', '').lower()
                endpoint = entry.get('from' if direction == 'outgoing' else 'to', '').lower()
                if (len(tx) != 66 or not tx.startswith('0x') or contract != VVV or endpoint != address
                        or not indexed['start'] <= block <= indexed['end']):
                    raise RuntimeError('Transfer index returned an unrelated transaction')
                if tx in processed:
                    continue
                receipt = rpc.receipt(tx)
                matched = [log for log in receipt if log['address'] == VVV
                           and len(log['topics']) == 3 and log['topics'][0] == TRANSFER
                           and topic_address(address) in log['topics'][1:]]
                if not matched or any(log['block'] != block for log in matched):
                    raise RuntimeError('Indexed transaction did not match its on-chain receipt')
                indexed['logs'].extend(matched)
                indexed['transactions'].append(tx)
                processed.add(tx)
                save()
            next_page = page.get('pageKey')
            if next_page and next_page == progress['page']:
                raise RuntimeError('Transfer index repeated a page key')
            progress.update(page=next_page, done=not next_page)
            save()
    # A completed indexed snapshot becomes the normal incremental log cache.
    unique = {(log['tx'], log['index']): log for log in indexed['logs'] if log['block'] >= start}
    cache['history'] = {'next_block': indexed['end'] + 1, 'logs': list(unique.values()), 'source': 'alchemy+rpc-receipts'}
    del cache['indexed_history']
    save()


def wallet_history(rpc, cache, address, start, latest, save):
    history = cache.get('history', {})
    if getattr(rpc, 'has_transfer_index', False) is True and not history.get('source'):
        try:
            indexed_history(rpc, cache, address, start, latest, save)
        except IndexUnavailable as exc:
            # A provider plan may not include the indexed API. Archive RPC remains
            # the verified fallback; don't pass the custom method to other RPCs.
            print(f'Transfer index unavailable; falling back to archive RPC: {exc}', flush=True)
            rpc.has_transfer_index = False
    return scan(rpc, cache, 'history', VVV,
                [[TRANSFER, topic_address(address)], [TRANSFER, None, topic_address(address)]],
                start, latest, save)


def main():
    rpc = RPC(float(os.environ.get('VVV_ACTIVITY_BUDGET_SECONDS', '900')))
    state_path = DATA / 'unstaker_activity_state.json'
    output_path = DATA / 'top_unstakers.json'
    state = load_json(state_path, {'version': 2, 'wallets': {}})
    if state.get('version') != 2:
        state = {'version': 2, 'wallets': {}}
    rpc.blocks = state.setdefault('block_timestamps', {})
    # Persist immutable receipt/transaction reads between indexing time slices.
    # Venice remains first choice for evidence; Alchemy discovers the tx hashes.
    rpc.receipts = state.setdefault('receipts', {})
    rpc.transactions = state.setdefault('transactions', {})
    rpc.tokens = state.setdefault('pool_tokens', {})
    rpc.codes = state.setdefault('contract_codes', {})
    save = lambda: atomic_json(state_path, state)
    if number(rpc.call('eth_chainId', [])) != 8453:
        raise RuntimeError('Activity RPC is not Base mainnet')
    latest = max(0, number(rpc.call('eth_blockNumber', [])) - 64)
    now = rpc.timestamp(latest)
    since = now - 90 * DAY
    try:
        ranking_start = rpc.first_block(now - 7 * DAY, latest)
        ranked_logs = scan(rpc, state, 'ranking', STAKING, [[INITIATED]], ranking_start, latest, save)
        selected, totals = rank_wallets(ranked_logs, rpc.timestamp, now, (ranking_start, latest))
    finally:
        save()
    previous = load_json(output_path, {})
    previous_wallets = {item['address']: item for item in previous.get('wallets', [])}
    output = {'schema_version': 2, 'as_of': utc(now), 'ranking_days': 7, 'history_days': 90,
              'ranking_complete': True, 'buckets': BUCKETS, 'wallets': [
                  {'address': address, 'initiated_amount': round(totals[address]['amount'] / UNIT, 8),
                   'initiation_count': totals[address]['count'], 'history_start': utc(since),
                   'status': 'indexing', 'progress': 0} for address in selected]}
    for item in output['wallets']:
        old = previous_wallets.get(item['address'], {})
        if previous.get('schema_version') == 2 and old.get('status') in ('complete', 'stale'):
            for field in ('series', 'claims', 'claim_count', 'unreconciled_outflow', 'history_start'):
                if field in old:
                    item[field] = old[field]
            item['history_as_of'] = old.get('history_as_of', previous['as_of'])
            item['status'] = 'stale'
    atomic_json(output_path, output)
    try:
        history_start = rpc.first_block(since, latest) if selected else latest
    finally:
        save()
    for item in output['wallets']:
        address = item['address']
        rpc.check()
        cache = state['wallets'].setdefault(address, {})
        try:
            logs = wallet_history(rpc, cache, address, history_start, latest, save)
            item['history_source'] = cache.get('history', {}).get('source', 'archive-rpc')
            moves = transfers(logs)
            opening_key = str(history_start)
            if cache.get('opening_block') != opening_key:
                cache['opening'] = number(rpc.call('eth_call', [
                    {'to': VVV, 'data': '0x70a08231' + topic_address(address)[2:]}, hex(max(0, history_start - 1))]))
                cache['opening_block'] = opening_key
            evidence = cache.setdefault('evidence', {})
            claims, sales, contracts, timestamps = {}, {}, set(), {}
            by_tx = defaultdict(list)
            for move in moves:
                by_tx[move['tx']].append(move)
                timestamps[str(move['block'])] = rpc.timestamp(move['block'])
                if len(timestamps) % 16 == 0:
                    save()
            for tx, tx_moves in by_tx.items():
                rpc.check()
                if tx not in evidence:
                    receipt = rpc.receipt(tx)
                    transaction = rpc.transaction(tx)
                    claim = verified_claim_amount(address, tx, receipt, transaction)
                    sold = verified_swap_amount(address, receipt, transaction, rpc.pool_tokens)
                    contract_dests = [move['to'] for move in tx_moves
                                      if move['from'] == address and move['to'] != STAKING
                                      and rpc.is_contract(move['to'], move['block'])]
                    evidence[tx] = {'claim': claim, 'sold': sold, 'contracts': contract_dests}
                    save()
                claims[tx] = evidence[tx]['claim']
                sales[tx] = evidence[tx]['sold']
                contracts.update(evidence[tx]['contracts'])
            result = analyze_wallet(address, logs, cache['opening'], claims, sales, contracts, timestamps, now, since)
            closing = number(rpc.call('eth_call', [
                {'to': VVV, 'data': '0x70a08231' + topic_address(address)[2:]}, hex(latest)]))
            if result.pop('balance_wei') != closing or result['unreconciled_outflow'] > 0:
                raise RuntimeError('Transfer replay does not reconcile with the on-chain VVV balance')
            item.update(result, status='complete', progress=100, history_as_of=utc(now), history_start=utc(since))
        except BudgetExpired:
            raise
        except Exception as exc:
            item.update(status='stale' if item['status'] == 'stale' else 'unavailable',
                        error='History could not be verified; retrying next refresh.')
            print(f'Wallet {address}: {exc}', flush=True)
        finally:
            next_block = cache.get('history', {}).get('next_block', history_start)
            if item['status'] != 'complete':
                item['progress'] = min(99, max(0, round(100 * (next_block - history_start) / max(1, latest - history_start))))
            atomic_json(output_path, output)
            save()
    # Keep only selected wallet caches; the rolling-ranking cache is independent.
    state['wallets'] = {address: state['wallets'][address] for address in selected}
    save()
    print(f'Published {len(selected)} ranked wallets as of {utc(now)}', flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Preserve previous public data if ranking itself fails. Never publish
        # empty history as zero sales, or label an interrupted scan "complete".
        print(f'Activity refresh incomplete: {exc}', flush=True)
        raise SystemExit(2)
