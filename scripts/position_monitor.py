#!/usr/bin/env python3
"""Receipt-verified Base position monitor, invoked by notify_unstaker.py.

Amounts are integer wei. Roots are pinned; recipients are followed for two hops
using FIFO attribution over ALL their VVV, including pre-existing balances.
Historical backfill and live scanning have independent, durable checkpoints.
"""
import argparse
import copy
import datetime as dt
import html
import json
import os
import re
import time
import urllib.request
from collections import defaultdict, deque
from decimal import Decimal, InvalidOperation
from pathlib import Path

import notify_unstaker as delivery
import unstaker_activity as chain

UNIT = chain.UNIT
DAY = 86400
APPROVAL = '0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925'
STAKED = '0x9e71bc8eea02a63969f509818f2dafb9254532904319f9dbda79b67bd34a5f3d'
ZERO = '0x' + '0' * 40
DEAD = '0x000000000000000000000000000000000000dead'
CONFIG = chain.DATA / 'position_watch.json'
EXCHANGES = re.compile(r'\b(bybit|binance|coinbase|kraken|okx|kucoin|mexc|bitget|bitfinex|gemini|htx|crypto\.com)\b', re.I)


class PositionRPC(chain.RPC):
    """Public archive RPC first for logs; authenticated index stays independent.

    Some authenticated plans stall on historical/wildcard log requests. The
    public endpoints used by the dashboard support our bounded 40k windows.
    Alchemy remains the indexed-transfer source and an ordinary RPC fallback.
    """
    def __init__(self, budget):
        super().__init__(budget)
        public = [(url, headers) for url, headers in self.providers
                  if url in (os.environ.get('PUBLICNODE_RPC_URL', 'https://base-rpc.publicnode.com'),
                             os.environ.get('BASE_RPC_URL', 'https://mainnet.base.org'))]
        self.providers = public + [provider for provider in self.providers if provider not in public]


def load_config(path=CONFIG):
    config = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    config['roots'] = {address.lower(): label for address, label in config['roots'].items()}
    if not config['roots'] or any(not re.fullmatch(r'0x[0-9a-f]{40}', address)
                                  for address in config['roots']):
        raise ValueError('Position roots must be valid Base addresses')
    for key in ('alert_vvv', 'alert_usd'):
        value = Decimal(config[key])
        if not value.is_finite() or value <= 0:
            raise ValueError(f'{key} must be positive')
    if not 0 <= config['max_hops'] <= 5 or not 0 <= config['daily_hour_utc'] <= 23:
        raise ValueError('Invalid hop limit or daily UTC hour')
    if config['start_block'] < 1:
        raise ValueError('start_block must be positive')
    for wallet, block in config.get('root_start_blocks', {}).items():
        if wallet not in config['roots'] or block < config['start_block']:
            raise ValueError('Invalid root-specific start block')
    return config


def amount(wei):
    return f'{Decimal(wei) / UNIT:,.6f}'.rstrip('0').rstrip('.')


def read_json_url(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'vvv-position-monitor'})
    with urllib.request.urlopen(request, timeout=12) as response:
        return json.load(response)


def explorer_label(title, address, now):
    title = html.unescape(re.sub(r'\s+', ' ', title)).strip()
    match = EXCHANGES.search(title)
    source = f'https://etherscan.io/address/{address}'
    if not match:
        return {'label': 'Owner unverified / no exchange name in explorer title',
                'entity': None, 'confidence': 'unverified', 'source': source,
                'source_chain': 'Ethereum; absence of a label does not prove private ownership',
                'checked_at': chain.utc(now), 'terminal': False}
    label = re.split(r'\s*\|\s*Address:', title, maxsplit=1, flags=re.I)[0][:180]
    return {'label': label, 'entity': match.group(1), 'confidence': 'explorer-tagged',
            'source': source, 'source_chain': 'Ethereum; exact-address label, not proof of current Base custody',
            'checked_at': chain.utc(now), 'terminal': bool(re.search(r'hot wallet', label, re.I))}


def refresh_labels(state, config, now):
    labels = state.setdefault('labels', {})
    for address, label in config.get('address_labels', {}).items():
        labels[address] = copy.deepcopy(label)
    deadline = time.monotonic() + 18
    for wallet, info in state['wallets'].items():
        label = labels.get(wallet)
        if label and label.get('terminal') and wallet not in config['roots']:
            info['terminal'] = True
        if wallet in config.get('address_labels', {}):
            continue  # Dated, manually source-checked evidence, never pretend it was refreshed.
        if label and now - label.get('fetched_at', now) < DAY:
            continue
        if time.monotonic() >= deadline:
            break
        try:
            url = f'https://etherscan.io/address/{wallet}'
            request = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 VVV investigation'})
            with urllib.request.urlopen(request, timeout=6) as response:
                page = response.read(1000000).decode('utf-8', errors='replace')
            title = re.search(r'<title>(.*?)</title>', page, re.I | re.S)
            if not title or 'etherscan' not in title.group(1).lower():
                raise ValueError('Explorer title unavailable')
            label = explorer_label(title.group(1), wallet, now)
        except Exception:
            label = {'label': 'Owner/exchange attribution unverified; explorer lookup unavailable',
                     'entity': None, 'confidence': 'unverified',
                     'source': f'https://etherscan.io/address/{wallet}',
                     'checked_at': None, 'terminal': False}
        label['fetched_at'] = now
        labels[wallet] = label
        if label.get('terminal') and wallet not in config['roots']:
            info['terminal'] = True


def attribution_lines(state):
    lines = ['\nExchange attribution (public labels and observed fund flow, not ownership proof):']
    for wallet in state['wallets']:
        label = state.get('labels', {}).get(wallet)
        if not label:
            lines.append(f'{wallet}: owner/exchange attribution unverified.')
            continue
        lines += [f'{wallet}: {label["label"]} [{label["confidence"]}]',
                  f'Source: {label["source"]}; checked {label.get("checked_at") or "unavailable"}. '
                  + label.get('source_chain', '')]
    lines.append('An exchange-linked destination does not identify the sender as the exchange. A deposit/sweep is not evidence of an exchange trade.')
    return lines


def select_quote(pairs, now):
    eligible = []
    for pair in pairs:
        if (pair.get('chainId') != 'base'
                or pair.get('baseToken', {}).get('address', '').lower() != chain.VVV):
            continue
        try:
            price = Decimal(str(pair.get('priceUsd')))
            liquidity = Decimal(str((pair.get('liquidity') or {}).get('usd', 0)))
        except InvalidOperation:
            continue
        if price.is_finite() and price > 0 and liquidity.is_finite() and liquidity >= 100000:
            eligible.append((liquidity, price, pair['pairAddress']))
    if not eligible:
        raise ValueError('No liquid Base VVV price market')
    _, price, pool = max(eligible)
    return {'usd': str(price), 'at': now, 'source': f'DexScreener / {pool}'}


def price_quote(state, now):
    try:
        quote = select_quote(read_json_url(
            f'https://api.dexscreener.com/token-pairs/v1/base/{chain.VVV}'), now)
        state['quote'] = quote
        return quote
    except Exception:
        cached = state.get('quote')
        if cached and 0 <= now - cached['at'] <= 1800:
            return cached
        return None


def meets_threshold(wei, quote, config):
    tokens = Decimal(wei) / UNIT
    # Inclusive boundaries are intentional: exactly 500 VVV also alerts.
    return (tokens >= Decimal(config['alert_vvv'])
            or (quote is not None and tokens * Decimal(quote['usd']) >= Decimal(config['alert_usd'])))


def new_state(config, head, now):
    return {'version': 2, 'boot_head': head, 'boot_at': now,
            'start_block': config['start_block'], 'wallets': {}, 'events': {},
            'outbox': [], 'baseline_queued': False, 'last_daily': None,
            'updated_at': None, 'quote': None, 'warnings': {}, 'labels': {}}


def wallet_entry(start, depth, head, parent=None, terminal=False, opening=0, opening_staked=0):
    return {'start_block': start, 'depth': depth, 'parent': parent,
            'terminal': terminal, 'opening_wei': opening, 'opening_staked_wei': opening_staked, 'cursor': start - 1,
            'live_cursor': max(start - 1, head), 'context_cursor': start - 1,
            'context_live_cursor': max(start - 1, head)}


def balance(rpc, contract, wallet, block):
    return chain.number(rpc.call('eth_call', [
        {'to': contract, 'data': '0x70a08231' + chain.topic_address(wallet)[2:]}, hex(block)]))


def add_roots(state, config):
    for wallet in config['roots']:
        if wallet not in state['wallets']:
            start = config.get('root_start_blocks', {}).get(wallet, config['start_block'])
            state['wallets'][wallet] = wallet_entry(
                start, 0, max(start - 1, state['boot_head'] - 200000))


def core_logs(rpc, wallet, start, end):
    topic = chain.topic_address(wallet)
    queries = ((chain.VVV, [chain.TRANSFER, topic]),
               (chain.VVV, [chain.TRANSFER, None, topic]),
               (chain.VVV, [APPROVAL, topic]),
               (chain.STAKING, [None, topic]))
    result = {}
    for contract, topics in queries:
        for log in delivery.fetch_logs(rpc, contract, topics, start, end):
            # Third parties can call transferFrom(..., 0). These are not flows.
            if log['topics'][0] == chain.TRANSFER and chain.number(log['data']) == 0:
                continue
            result[(log['tx'], log['index'])] = log
    return sorted(result.values(), key=lambda log: (log['block'], log['index']))


def same_log(a, b):
    return all(a[field] == b[field] for field in ('tx', 'block', 'index', 'address', 'topics', 'data'))


def ingest(state, rpc, tx_hash, now, evidence=()):
    tx_hash = tx_hash.lower()
    if tx_hash not in state['events']:
        raw = rpc.call('eth_getTransactionReceipt', [tx_hash])
        tx = rpc.transaction(tx_hash)
        if (raw['transactionHash'].lower() != tx_hash or tx['hash'].lower() != tx_hash
                or raw['blockHash'].lower() != tx['blockHash'].lower()
                or chain.number(raw['blockNumber']) != chain.number(tx['blockNumber'])):
            raise RuntimeError('Mismatched transaction/receipt')
        logs = sorted((chain.normalize(log) for log in raw['logs']), key=lambda log: log['index'])
        block = chain.number(raw['blockNumber'])
        if any(log['tx'] != tx_hash or log['block'] != block for log in logs):
            raise RuntimeError('Receipt contains mismatched logs')
        state['events'][tx_hash] = {
            'hash': tx_hash, 'block': block, 'index': chain.number(tx['transactionIndex']),
            'at': rpc.timestamp(block), 'observed_at': now,
            'success': chain.number(raw.get('status', '0x0')) == 1,
            'transaction': {key: tx.get(key) for key in ('hash', 'from', 'to', 'value', 'input')},
            'logs': logs, 'context': {}, 'alerted': False, 'reported': False, 'quote': None}
    event = state['events'][tx_hash]
    for log in evidence:
        if not any(same_log(log, receipt_log) for receipt_log in event['logs']):
            raise RuntimeError('Filtered log does not match the transaction receipt')
    return event


def scan_core(state, rpc, wallet, start, end, now):
    grouped = defaultdict(list)
    for log in core_logs(rpc, wallet, start, end):
        grouped[log['tx']].append(log)
    for tx_hash, evidence in grouped.items():
        ingest(state, rpc, tx_hash, now, evidence)


def scan_context(state, rpc, wallet, start, end, now):
    if not rpc.index_provider:
        raise chain.IndexUnavailable('Alchemy transfer index is not configured')
    for direction in ('fromAddress', 'toAddress'):
        page = None
        pages = set()
        while True:
            options = {'fromBlock': hex(start), 'toBlock': hex(end), direction: wallet,
                       'category': ['external', 'internal', 'erc20', 'erc721', 'erc1155'],
                       'excludeZeroValue': False, 'withMetadata': True,
                       'maxCount': '0x3e8', 'order': 'asc'}
            if page:
                options['pageKey'] = page
            result = rpc.call('alchemy_getAssetTransfers', [options])
            if not isinstance(result, dict) or not isinstance(result.get('transfers'), list):
                raise RuntimeError('Invalid context transfer response')
            for item in result['transfers']:
                src, dst = (item.get('from') or '').lower(), (item.get('to') or '').lower()
                block = chain.number(item['blockNum'])
                if not start <= block <= end or wallet not in (src, dst):
                    raise RuntimeError('Context index returned an out-of-range transfer')
                raw = item.get('rawContract') or {}
                # Zero ERC20/NFT spam is not evidence of wallet-signed activity.
                if item['category'] == 'erc20' and raw.get('value') and chain.number(raw['value']) == 0:
                    continue
                event = ingest(state, rpc, item['hash'], now)
                if event['block'] != block:
                    raise RuntimeError('Indexed transfer block does not match receipt')
                key = item.get('uniqueId') or json.dumps(item, sort_keys=True)
                if key not in event['context']:
                    event['reported'] = False  # Include newly discovered context next daily report.
                event['context'][key] = {key: item.get(key) for key in
                    ('category', 'from', 'to', 'asset', 'value', 'rawContract',
                     'tokenId', 'erc1155Metadata')}
            page = result.get('pageKey')
            if not page:
                break
            if page in pages:
                raise RuntimeError('Context index repeated its page cursor')
            pages.add(page)


def consume(lots, quantity):
    """Consume untracked opening/incoming balances before later tracked lots."""
    pieces = []
    while quantity and lots:
        available, depth = lots[0]
        take = min(quantity, available)
        pieces.append((take, depth))
        quantity -= take
        lots[0][0] -= take
        if not lots[0][0]:
            lots.popleft()
    if quantity:
        pieces.append((quantity, None))
    return pieces


def trace_events(state, config):
    """Replay receipt-ordered VVV; never reset a recipient's hop count on reuse.

    Root balances are the monitored position. Recipient attribution is FIFO,
    including balances already present before the first linked receipt.
    Contracts/burn addresses are terminal destinations, not new wallet owners.
    """
    roots = set(config['roots'])
    wallets = state['wallets']
    lots = defaultdict(deque)
    restaked = defaultdict(deque)
    for wallet, info in wallets.items():
        if info['opening_wei']:
            lots[wallet].append([info['opening_wei'], None])
        if info.get('opening_staked_wei'):
            restaked[wallet].append([info['opening_staked_wei'], None])
    discovered = {}
    movements = {}
    edges = {}
    for event in sorted(state['events'].values(), key=lambda e: (e['block'], e['index'])):
        incoming, outgoing = defaultdict(int), defaultdict(int)
        initiated = defaultdict(int)
        claims = {}
        for wallet in wallets:
            claims[wallet] = chain.verified_claim_amount(
                wallet, event['hash'], event['logs'], event['transaction']) if event['success'] else 0
        for move in chain.transfers(event['logs']):
            if move['address'] != chain.VVV or not move['amount'] or move['from'] == move['to']:
                continue
            src, dst, quantity = move['from'], move['to'], move['amount']
            if src in roots:
                pieces = [(quantity, 0)]
            elif src == chain.STAKING and claims.get(dst):
                released = min(quantity, claims[dst])
                pieces = consume(restaked[dst], released) + [(quantity - released, None)]
                claims[dst] -= released
            else:
                pieces = consume(lots[src], quantity)
            carried = sum(value for value, depth in pieces if depth is not None)
            if src in roots or src in wallets:
                outgoing[src] += quantity if src in roots else carried
            if dst in roots:
                incoming[dst] += quantity
            if dst == chain.STAKING and src in wallets and src not in roots:
                restaked[src].extend([value, depth] for value, depth in pieces if value)
            if dst in roots:
                continue
            for value, depth in pieces:
                if not value:
                    continue
                # A verified claim restores its previous depth, not another hop.
                next_depth = depth if src == chain.STAKING else depth + 1 if depth is not None else None
                terminal = (dst in (ZERO, DEAD, chain.STAKING, chain.VVV)
                            or wallets.get(src, {}).get('terminal', False))
                followed = (next_depth is not None and next_depth <= config['max_hops']
                            and not terminal)
                if followed:
                    info = {'depth': next_depth, 'parent': src, 'block': event['block']}
                    previous = discovered.get(dst)
                    if previous is None:
                        discovered[dst] = info
                    else:
                        if event['block'] < previous['block']:
                            previous.update(block=event['block'], parent=src)
                        previous['depth'] = min(previous['depth'], next_depth)
                    incoming[dst] += value
                    edge_key = (src, dst)
                    edges.setdefault(edge_key, {'amount': 0, 'txs': []})
                    edges[edge_key]['amount'] += value
                    if event['hash'] not in edges[edge_key]['txs']:
                        edges[edge_key]['txs'].append(event['hash'])
                lots[dst].append([value, next_depth if followed else None])
        for log in event['logs']:
            if (log['address'] == chain.STAKING and len(log['topics']) == 2
                    and log['topics'][0] == chain.INITIATED):
                wallet = '0x' + log['topics'][1][-40:]
                values = chain.words(log['data'])
                if len(values) == 1:
                    tracked_stake = sum(value for value, depth in restaked[wallet] if depth is not None)
                    initiated[wallet] += values[0] if wallet in roots else min(values[0], tracked_stake)
        eligible = {}
        for wallet in set(incoming) | set(outgoing) | set(initiated):
            if wallet not in wallets:
                continue
            info = wallets[wallet]
            if info['terminal']:
                continue
            # Do not attribute a recipient's later activity until its opening-to-
            # event history is contiguous; live roots can alert during backfill.
            complete = (wallet in roots or info['cursor'] >= event['block']
                        or (info['start_block'] > state['boot_head'] and info['live_cursor'] >= event['block']))
            if complete:
                eligible[wallet] = max(incoming[wallet], outgoing[wallet], initiated[wallet])
        movements[event['hash']] = eligible
    return discovered, movements, edges, lots


def expand_wallets(state, config, rpc):
    changed = True
    while changed:
        changed = False
        discovered, _, _, _ = trace_events(state, config)
        for wallet, info in discovered.items():
            parent = info['parent']
            if parent not in state['wallets'] or state['wallets'][parent]['terminal']:
                continue
            if wallet in state['wallets']:
                existing = state['wallets'][wallet]
                existing['depth'] = min(existing['depth'], info['depth'])
                if info['block'] < existing['start_block']:
                    existing['start_block'] = info['block']
                    existing['cursor'] = info['block'] - 1
                    existing['context_cursor'] = min(existing['context_cursor'], info['block'] - 1)
                    existing['opening_wei'] = balance(rpc, chain.VVV, wallet, info['block'] - 1)
                    existing['opening_staked_wei'] = balance(rpc, chain.STAKING, wallet, info['block'] - 1)
                    existing['parent'] = parent
                    changed = True
                continue
            terminal = rpc.is_contract(wallet, info['block'])
            opening = balance(rpc, chain.VVV, wallet, info['block'] - 1)
            opening_staked = 0 if terminal else balance(rpc, chain.STAKING, wallet, info['block'] - 1)
            state['wallets'][wallet] = wallet_entry(
                info['block'], info['depth'], info['block'] - 1,
                parent, terminal, opening, opening_staked)
            changed = True


def event_description(event, wallets, rpc=None, labels=None):
    lines = []
    tx = event['transaction']
    sender = (tx.get('from') or '').lower()
    if not event['success']:
        return [f'Failed transaction from {sender} to {tx.get("to")}']
    claims = {wallet: chain.verified_claim_amount(wallet, event['hash'], event['logs'], tx)
              for wallet in wallets}
    for move in chain.transfers(event['logs']):
        src, dst = move['from'], move['to']
        if src not in wallets and dst not in wallets or not move['amount']:
            continue
        if move['address'] == chain.VVV:
            kind = ('Verified unstake claim' if src == chain.STAKING and claims.get(dst)
                    else 'Staking reward/other payment' if src == chain.STAKING
                    else 'Staked VVV' if dst == chain.STAKING else 'VVV transfer')
            lines.append(f'{kind}: {amount(move["amount"])} VVV\n{src} -> {dst}')
            label = (labels or {}).get(dst)
            if label and label.get('entity'):
                lines.append(f'Destination: {label["label"]} [{label["confidence"]}]; {label["source"]}. Exchange deposit/transfer, not verified sale.')
        elif move['address'] == chain.STAKING:
            lines.append(f'sVVV transfer: {amount(move["amount"])} sVVV\n{src} -> {dst}')
        else:
            lines.append(f'Other ERC20 transfer (raw units {move["amount"]}; token {move["address"]})\n{src} -> {dst}')
    for log in event['logs']:
        topics = log['topics']
        if not topics:
            continue
        if log['address'] == chain.STAKING and topics[0] in (chain.INITIATED, chain.FINALIZED, STAKED) and len(topics) >= 2:
            wallet = '0x' + topics[1][-40:]
            values = chain.words(log['data'])
            if wallet in wallets and values:
                label = {chain.INITIATED: 'Unstake initiated', chain.FINALIZED: 'Unstake finalized', STAKED: 'Stake event'}[topics[0]]
                lines.append(f'{label}: {amount(values[0])} VVV / {wallet}')
        if topics[0] == APPROVAL and len(topics) == 3:
            owner = '0x' + topics[1][-40:]
            if owner in wallets:
                spender = '0x' + topics[2][-40:]
                lines.append(f'Approval: {owner} -> {spender}; token {log["address"]}; raw allowance {chain.number(log["data"])}')
    if sender in wallets:
        native = chain.number(tx.get('value') or '0x0')
        if native:
            lines.append(f'Native ETH sent: {amount(native)} / {sender} -> {tx.get("to")}')
        if not lines:
            lines.append(f'Wallet call: {sender} -> {tx.get("to")}; method {(tx.get("input") or "0x")[:10]}')
    for item in event.get('context', {}).values():
        if item['category'] == 'erc20':
            continue  # Receipt transfers above, rather than indexed token labels.
        if item['category'] in ('external', 'internal') and not item.get('value'):
            continue
        lines.append(f'Indexed context ({item["category"]}): {item.get("value")} {str(item.get("asset") or "asset")[:40]} / '
                     f'{item.get("from")} -> {item.get("to")} (index label, not sale evidence)')
        if item['category'] in ('erc721', 'erc1155'):
            lines.append(f'NFT contract {(item.get("rawContract") or {}).get("address")}; '
                         f'token ID {item.get("tokenId")}; quantities {item.get("erc1155Metadata")}')
    if rpc:
        for wallet in wallets:
            if sender != wallet:
                continue
            sold = chain.verified_swap_amount(wallet, event['logs'], tx, rpc.pool_tokens)
            if sold:
                lines.append(f'Sold via verified swap: {amount(sold)} VVV')
    return lines or ['Indexed activity; no supported VVV movement established.']


def chunks(lines, limit=3600):
    pages, text = [], ''
    for line in lines:
        # Telegram has a 4096-character limit; preserve every detail across pages.
        for offset in range(0, max(1, len(line)), limit):
            piece = line[offset:offset + limit]
            if text and len(text) + len(piece) + 1 > limit:
                pages.append(text)
                text = ''
            text += ('\n' if text else '') + piece
    if text:
        pages.append(text)
    return pages


def queue(state, key, lines):
    if any(item['id'].startswith(key + ':') for item in state['outbox']):
        return
    pages = chunks(lines)
    entries = []
    for index, text in enumerate(pages):
        if len(pages) > 1:
            text = f'{lines[0]} — {index + 1}/{len(pages)}\n{text}'
        entries.append({'id': f'{key}:{index}', 'text': text})
    if key.startswith('alert:'):
        index = next((i for i, item in enumerate(state['outbox'])
                      if not item['id'].startswith('alert:')), len(state['outbox']))
        state['outbox'][index:index] = entries
    else:
        state['outbox'].extend(entries)


def flush(state, save, token, chat_id, dry_run=False, limit=20):
    for index in range(min(limit, len(state['outbox']))):
        if index and not dry_run:
            time.sleep(1.05)  # Respect Telegram's per-chat message rate.
        item = state['outbox'][0]
        delivery.send(token, chat_id, item['text'], dry_run)
        if not dry_run:
            print(f'Telegram delivered {item["id"]} ({len(item["text"])} characters)', flush=True)
        state['outbox'].pop(0)
        save()


def snapshot(state, rpc, head):
    result = {}
    for wallet, info in state['wallets'].items():
        if info['terminal']:
            continue
        pending = chain.words(rpc.call('eth_call', [
            {'to': chain.STAKING, 'data': '0x16934fc4' + chain.topic_address(wallet)[2:]}, hex(head)]))
        if len(pending) != 3:
            raise RuntimeError('Unexpected stakes(address) layout')
        result[wallet] = {'liquid': balance(rpc, chain.VVV, wallet, head),
                          'staked': balance(rpc, chain.STAKING, wallet, head),
                          'cooldown_end': pending[1], 'pending': pending[2]}
    return result


def report_lines(state, config, rpc, head, now, baseline=False):
    title = 'VVV position — baseline investigation' if baseline else 'VVV position — daily investigation'
    lines = [title, f'Report time: {chain.utc(now)}', f'Confirmed through block {head:,} / {chain.utc(rpc.timestamp(head))}',
             f'Movement alerts: >= {config["alert_vvv"]} VVV OR >= ${Decimal(config["alert_usd"]):,.0f}; per wallet/transaction.',
             f'Pinned roots: {len(config["roots"])}; onward wallet hops: {config["max_hops"]}.']
    if not core_complete(state):
        lines.append('Historical indexing is in progress. Totals below are partial, not a complete position reconciliation; live checks run first.')
    quote = state.get('quote')
    lines.append(f'Latest valuation: ${quote["usd"]}/VVV at {chain.utc(quote["at"])} ({quote["source"]})'
                 if quote and now - quote['at'] <= 1800 else 'USD price unavailable/stale; token threshold remains active.')
    balances = snapshot(state, rpc, head)
    _, _, _, attributed_lots = trace_events(state, config)
    reconciled = True
    for wallet, values in balances.items():
        info = state['wallets'][wallet]
        label = config['roots'].get(wallet, f'Linked recipient, hop {info["depth"]}')
        lines += [f'\n{label}: {wallet}',
                  f'Whole-wallet balances: liquid {amount(values["liquid"])} VVV; staked {amount(values["staked"])} sVVV; pending unstake {amount(values["pending"])} VVV.']
        if wallet not in config['roots']:
            attributed = sum(value for value, depth in attributed_lots[wallet] if depth is not None)
            lines.append(f'FIFO-attributed liquid position here: {amount(attributed)} VVV (other wallet funds are separate).')
        if values['pending']:
            lines.append(f'Cooldown ends {chain.utc(values["cooldown_end"])}; expiry does not prove a claim.')
        through = max(info['cursor'], info['start_block'] - 1)
        lines.append(f'History: block {info["start_block"]:,}–{through:,}; live through {info["live_cursor"]:,}.')
        if info['cursor'] >= min(head, state['boot_head']) and info['live_cursor'] >= head:
            opening = balance(rpc, chain.VVV, wallet, info['start_block'] - 1)
            delta = 0
            for event in state['events'].values():
                if not info['start_block'] <= event['block'] <= head:
                    continue
                for move in chain.transfers(event['logs']):
                    if move['address'] == chain.VVV:
                        delta += move['amount'] if move['to'] == wallet else 0
                        delta -= move['amount'] if move['from'] == wallet else 0
            if opening + delta == values['liquid']:
                lines.append(f'Balance reconciliation verified: opening {amount(opening)} + net flow {amount(delta)} = {amount(values["liquid"])} VVV.')
                state['warnings'].pop(f'reconcile:{wallet}', None)
            else:
                reconciled = False
                state['warnings'][f'reconcile:{wallet}'] = f'VVV history for {wallet} does not reconcile; replaying the history before calling it complete.'
                # Requery the missing history; existing receipts remain deduplicated.
                info['cursor'] = info['start_block'] - 1
                lines.append('Balance reconciliation incomplete; history is being rechecked.')
        if info['context_cursor'] < min(state['boot_head'], head) or info['context_live_cursor'] < head:
            lines.append('Other-asset context incomplete; core VVV/staking coverage shown separately.')
    lines += attribution_lines(state)
    selected = [event for event in state['events'].values() if baseline or not event['reported']]
    selected.sort(key=lambda event: (event['block'], event['index']))
    lines.append(f'\nActivity included: {len(selected)} transaction(s), including below-threshold activity.')
    initiated = claimed = sent = received = 0
    original = next(iter(config['roots']))
    for event in sorted(state['events'].values(), key=lambda event: (event['block'], event['index'])):
        claimed += chain.verified_claim_amount(original, event['hash'], event['logs'], event['transaction'])
        for log in event['logs']:
            if (log['address'] == chain.STAKING and log['topics'] == [chain.INITIATED, chain.topic_address(original)]):
                initiated += chain.words(log['data'])[0]
        for move in chain.transfers(event['logs']):
            if move['address'] == chain.VVV:
                sent += move['amount'] if move['from'] == original else 0
                received += move['amount'] if move['to'] == original else 0
    lines.append(f'Original wallet, indexed so far: initiated {amount(initiated)}; verified claims {amount(claimed)}; '
                 f'all inflows {amount(received)}; all outflows {amount(sent)} VVV. Inflows include unrelated receipts/rewards.')
    for event in selected:
        lines += [f'\n{chain.utc(event["at"])} / block {event["block"]:,}',
                  *event_description(event, state['wallets'], rpc, state.get('labels')),
                  f'https://basescan.org/tx/{event["hash"]}']
    if not selected:
        lines.append('No newly indexed wallet activity since the previous report.')
    for warning in state['warnings'].values():
        lines.append(f'Coverage note: {warning}')
    lines += ['\nInterpretation: transfers are not sales; linked addresses do not prove shared ownership.',
              'Recipient attribution uses FIFO including prior/unrelated VVV. Contracts, pools and burn addresses are terminal endpoints; cross-chain continuation is not indexed.',
              'Other assets/native internal movements use indexed context; arbitrary calls without indexed transfers may not be discoverable. Zero-value token spam is excluded.']
    return lines, selected, reconciled


def queue_alerts(state, config, rpc, quote, now):
    _, movements, _, _ = trace_events(state, config)
    for event in sorted(state['events'].values(), key=lambda event: (event['block'], event['index'])):
        if event['block'] <= state['boot_head'] or event['alerted']:
            continue
        candidates = movements[event['hash']]
        quantity = max(candidates.values(), default=0)
        if not quantity:
            continue
        if event['quote'] is None and quote:
            event['quote'] = copy.deepcopy(quote)
        valuation = event['quote']
        if not meets_threshold(quantity, valuation, config):
            continue
        lines = ['VVV position — sizeable movement', f'Activity time: {chain.utc(event["at"])}',
                 f'Largest qualifying wallet movement: {amount(quantity)} VVV.']
        if valuation:
            usd = Decimal(quantity) / UNIT * Decimal(valuation['usd'])
            lines.append(f'Estimated value: ${usd:,.2f}; ${valuation["usd"]}/VVV quote at {chain.utc(valuation["at"])} ({valuation["source"]}). Not execution proceeds.')
        else:
            lines.append('USD valuation unavailable; the VVV amount meets the token threshold.')
        lines += event_description(event, state['wallets'], rpc, state.get('labels'))
        # Balances are from one confirmed block, not an unpinned latest read.
        for wallet, value in candidates.items():
            if value:
                remaining = balance(rpc, chain.VVV, wallet, event['block'])
                lines.append(f'{wallet}: closing block balance {amount(remaining)} VVV.')
        lines += ['Transfers alone do not prove a sale or common ownership.',
                  f'https://basescan.org/tx/{event["hash"]}']
        queue(state, f'alert:{event["hash"]}', lines)
        event['alerted'] = True


def core_complete(state):
    head = state.get('scan_head', state['boot_head'])
    return all(info['terminal'] or (info['cursor'] >= min(head, state['boot_head'])
                                   and info['live_cursor'] >= head)
               for info in state['wallets'].values())


def daily_key(now, hour):
    return dt.datetime.fromtimestamp(now - hour * 3600, dt.timezone.utc).date().isoformat()


def run(state, config, rpc, head, now, save, quote):
    state['scan_head'] = head
    add_roots(state, config)
    for tx_hash in config.get('seed_transactions', []):
        event = ingest(state, rpc, tx_hash, now)
        if event['block'] > head:
            raise RuntimeError('Seed transaction is newer than confirmed head')
    expand_wallets(state, config, rpc)
    refresh_labels(state, config, now)
    save()
    # Live checks always precede the historical work. New recipients are scanned
    # from their first linked block, including movements within the same poll.
    processed = set()
    while True:
        pending = [wallet for wallet, info in state['wallets'].items()
                   if not info['terminal'] and wallet not in processed]
        if not pending:
            break
        for wallet in pending:
            info = state['wallets'][wallet]
            while info['live_cursor'] < head:
                start = info['live_cursor'] + 1
                stop = min(head, start + rpc.chunk - 1)
                scan_core(state, rpc, wallet, start, stop, now)
                info['live_cursor'] = stop
                # A recipient first discovered after boot has contiguous history.
                if info['cursor'] == start - 1:
                    info['cursor'] = stop
                expand_wallets(state, config, rpc)
                # Source-checked hot wallets are endpoints: their unrelated
                # omnibus inventory is not added to the monitored position.
                for address, label in state.get('labels', {}).items():
                    if label.get('terminal') and address in state['wallets'] and address not in config['roots']:
                        state['wallets'][address]['terminal'] = True
                print(f'Live VVV checkpoint {wallet}: {stop:,}', flush=True)
                save()
            processed.add(wallet)
            queue_alerts(state, config, rpc, quote, now)
            save()
    queue_alerts(state, config, rpc, quote, now)
    save()
    # Time-box old history separately, leaving time to queue reports and deliver.
    history_deadline = min(rpc.deadline - 50, time.monotonic() + 120)
    full_deadline = rpc.deadline
    rpc.deadline = history_deadline
    try:
        for wallet in list(state['wallets']):
            info = state['wallets'][wallet]
            while not info['terminal'] and info['cursor'] < head and time.monotonic() < history_deadline:
                start = info['cursor'] + 1
                stop = min(head, start + rpc.chunk - 1)
                scan_core(state, rpc, wallet, start, stop, now)
                info['cursor'] = stop
                expand_wallets(state, config, rpc)
                save()
    except chain.BudgetExpired:
        pass  # Partial windows are retried; live monitoring has already run.
    finally:
        rpc.deadline = full_deadline
    # Context is non-blocking for VVV alerts and has its own retryable cursors.
    for wallet, info in list(state['wallets'].items()):
        if info['terminal'] or time.monotonic() >= history_deadline:
            continue
        rpc.deadline = history_deadline
        try:
            for cursor_name in ('context_live_cursor', 'context_cursor'):
                if info[cursor_name] >= head:
                    continue
                start = info[cursor_name] + 1
                stop = min(head, start + (200000 if cursor_name == 'context_cursor' else rpc.chunk) - 1)
                scan_context(state, rpc, wallet, start, stop, now)
                info[cursor_name] = stop
                save()
            state['warnings'].pop(f'context:{wallet}', None)
        except chain.BudgetExpired:
            state['warnings'][f'context:{wallet}'] = f'Other-asset discovery for {wallet} is indexing; it will resume next poll.'
        except Exception:
            state['warnings'][f'context:{wallet}'] = f'Other-asset discovery for {wallet} is incomplete; it will retry.'
        finally:
            rpc.deadline = full_deadline
    expand_wallets(state, config, rpc)
    refresh_labels(state, config, now)
    queue_alerts(state, config, rpc, quote, now)
    key = daily_key(now, config['daily_hour_utc'])
    baseline = not state['baseline_queued'] and core_complete(state)
    if baseline or state['last_daily'] != key:
        lines, selected, reconciled = report_lines(state, config, rpc, head, now, baseline)
        if not reconciled:
            lines[0] += ' (reconciliation incomplete)'
        queue(state, f'baseline:{state["boot_head"]}' if baseline and reconciled else f'daily:{key}', lines)
        for event in selected:
            event['reported'] = True
        state['last_daily'] = key
        if baseline and reconciled:
            state['baseline_queued'] = True
    state['updated_at'] = now
    save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true', help='Read-only: no state writes or Telegram sends')
    parser.add_argument('--test', action='store_true')
    parser.add_argument('--config', default=str(CONFIG))
    parser.add_argument('--state', default=os.environ.get('UNSTAKER_ALERT_STATE', '.alert-state/position.json'))
    args = parser.parse_args()
    config = load_config(args.config)
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
    chat_id = os.environ.get('TELEGRAM_CHAT_ID', '').strip()
    if not args.dry_run and (not token or not chat_id):
        raise SystemExit('TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required')
    if args.test:
        delivery.send(token, chat_id, 'VVV position monitor test.\nPinned wallets: '
                      + ', '.join(config['roots'])
                      + f'\nAlerts: >= {config["alert_vvv"]} VVV OR >= ${config["alert_usd"]}.', args.dry_run)
        return
    path = Path(args.state)
    state = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else None
    if state and (state.get('version') != 2 or state['start_block'] != config['start_block']):
        raise SystemExit('Unexpected position state version/start block; preserve it and migrate explicitly')

    def save():
        if not args.dry_run:
            delivery.save_state(path, state)

    # Previously queued messages get a delivery attempt even if an RPC is down.
    if state:
        flush(state, save, token, chat_id, args.dry_run)
    rpc = PositionRPC(float(os.environ.get('VVV_ALERT_BUDGET_SECONDS', '420')))
    confirmations = max(0, int(os.environ.get('VVV_ALERT_CONFIRMATIONS', '64')))
    head = max(0, chain.number(rpc.call('eth_blockNumber', [])) - confirmations)
    now = int(time.time())
    if state is None:
        state = new_state(config, head, now)
    quote = price_quote(state, now)
    try:
        run(state, config, rpc, head, now, save, quote)
    finally:
        save()
        flush(state, save, token, chat_id, args.dry_run)
    print(f'Position monitor: {len(state["wallets"])} addresses, {len(state["events"])} transactions; '
          f'{len(state["outbox"])} Telegram page(s) pending; baseline complete={core_complete(state)}', flush=True)


if __name__ == '__main__':
    main()
