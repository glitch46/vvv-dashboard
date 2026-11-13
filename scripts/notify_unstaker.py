#!/usr/bin/env python3
"""Alert a Telegram chat when a tracked large unstaker moves VVV.

Designed to run on a schedule (see .github/workflows/unstaker-alert.yml). It
reuses the RPC plumbing and verification rules in unstaker_activity.py, so an
alert only labels something a sale when the swap evidence checks out on-chain.

Environment:
    TELEGRAM_BOT_TOKEN       Bot token from @BotFather (required to send).
    TELEGRAM_CHAT_ID         Destination chat. Discovered from getUpdates when
                             absent, then persisted in the state file.
    UNSTAKER_WALLETS         Comma-separated addresses. Defaults to the top
                             ranked 30-day initiator in data/top_unstakers.json.
    UNSTAKER_ALERT_STATE     State file (default .alert-state/state.json).
    VVV_ALERT_CONFIRMATIONS  Blocks to lag the chain head (default 64).

Usage:
    python scripts/notify_unstaker.py                # poll and alert
    python scripts/notify_unstaker.py --dry-run      # print instead of send
    python scripts/notify_unstaker.py --test         # send one test message
    python scripts/notify_unstaker.py --lookback 20000   # scan recent blocks
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

import unstaker_activity as activity

UNIT = activity.UNIT
DATA = activity.DATA
SEEN_LIMIT = 2000
MESSAGE_LIMIT = 20


def load_state(path):
    if not path.exists():
        return {'version': 1, 'chat_id': None, 'cursor_block': None, 'seen': []}
    return json.loads(path.read_text(encoding='utf-8-sig'))


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(state, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def short(address):
    return f'{address[:6]}...{address[-4:]}'


def format_amount(wei):
    value = wei / UNIT
    if value >= 1000:
        return f'{value:,.0f}'
    if value >= 1:
        return f'{value:,.2f}'
    return (f'{value:,.6f}').rstrip('0').rstrip('.')


def telegram(method, payload, token):
    request = urllib.request.Request(
        f'https://api.telegram.org/bot{token}/{method}',
        data=json.dumps(payload).encode(),
        headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
    except Exception as exc:
        raise RuntimeError(f'Telegram {method} failed: {exc}') from exc
    if not result.get('ok'):
        raise RuntimeError(f'Telegram {method} failed: {result.get("description")}')
    return result['result']


def discover_chat(token):
    # offset=-1 asks for the most recent update, so a single /start is enough.
    updates = telegram('getUpdates', {'offset': -1, 'limit': 1}, token)
    for update in reversed(updates):
        message = update.get('message') or update.get('edited_message') or {}
        chat_id = (message.get('chat') or {}).get('id')
        if chat_id:
            return chat_id
    return None


def send(token, chat_id, text, dry_run=False):
    if dry_run:
        print(f'--- message to {chat_id} ---\n{text}\n', flush=True)
        return
    telegram('sendMessage', {'chat_id': chat_id, 'text': text,
                             'disable_web_page_preview': True}, token)


def tracked_wallets():
    configured = os.environ.get('UNSTAKER_WALLETS', '').strip()
    if configured:
        addresses = [item.strip().lower() for item in configured.split(',') if item.strip()]
    else:
        path = DATA / 'top_unstakers.json'
        data = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        addresses = [item['address'].lower() for item in data.get('wallets', [])[:1]]
    for address in addresses:
        if len(address) != 42 or not address.startswith('0x'):
            raise SystemExit(f'Invalid wallet address: {address}')
    if not addresses:
        raise SystemExit('No wallet to track: set UNSTAKER_WALLETS')
    return addresses


def fetch_logs(rpc, address, topics, start, end):
    result = []
    cursor = start
    while cursor <= end:
        stop = min(end, cursor + rpc.chunk - 1)
        try:
            result.extend(rpc.logs(address, topics, cursor, stop))
        except Exception as exc:
            if (any(word in str(exc).lower() for word in ('range', 'limit', 'too many results'))
                    and rpc.chunk > 250):
                rpc.chunk = max(250, rpc.chunk // 2)
                continue
            raise
        cursor = stop + 1
    return result


def wallet_logs(rpc, wallet, start, end):
    topic = activity.topic_address(wallet)
    merged = {}
    queries = (
        (activity.STAKING, [activity.INITIATED, topic]),
        (activity.STAKING, [activity.FINALIZED, topic]),
        (activity.VVV, [activity.TRANSFER, topic]),
        (activity.VVV, [activity.TRANSFER, None, topic]),
    )
    for address, topics in queries:
        for log in fetch_logs(rpc, address, topics, start, end):
            merged[(log['tx'], log['index'])] = log
    return sorted(merged.values(), key=lambda log: (log['block'], log['index']))


def verified_sale(wallet, tx, logs, rpc):
    """A sale needs the full receipt; the filtered wallet logs are not enough."""
    try:
        return activity.verified_swap_amount(
            wallet, rpc.receipt(tx), rpc.transaction(tx), rpc.pool_tokens)
    except Exception as exc:
        print(f'Could not verify swap in {tx}: {exc}', flush=True)
        return 0


def summarize(wallet, logs, rpc):
    initiated = finalized = 0
    incoming = []
    outgoing = []
    for log in logs:
        if log['address'] == activity.STAKING:
            signature = log['topics'][0] if log['topics'] else None
            try:
                values = activity.words(log['data'])
            except ValueError:
                values = []
            if signature == activity.INITIATED:
                initiated += values[0] if values else 0
            elif signature == activity.FINALIZED:
                finalized += sum(values) if values else 0
        elif log['address'] == activity.VVV and len(log['topics']) == 3:
            if log['topics'][0] != activity.TRANSFER:
                continue
            source = '0x' + log['topics'][1][-40:]
            destination = '0x' + log['topics'][2][-40:]
            amount = activity.number(log['data'])
            if destination == wallet and source != wallet:
                incoming.append((source, amount))
            elif source == wallet and destination != wallet:
                outgoing.append((destination, amount))
    claimed = sum(amount for source, amount in incoming if source == activity.STAKING)
    staked = sum(amount for destination, amount in outgoing if destination == activity.STAKING)
    transferred = sum(amount for destination, amount in outgoing if destination != activity.STAKING)
    sold = verified_sale(wallet, logs[0]['tx'], logs, rpc) if outgoing else 0
    transferred = max(0, transferred - sold)
    received = [(source, amount) for source, amount in incoming if source != activity.STAKING]
    return {'initiated': initiated, 'finalized': finalized, 'claimed': claimed,
            'staked': staked, 'transferred': transferred, 'sold': sold, 'received': received}


def message(wallet, logs, summary):
    lines = ['Large unstaker activity', short(wallet)]
    if summary['initiated']:
        lines.append(f'Unstake initiated: {format_amount(summary["initiated"])} VVV')
    if summary['finalized'] and not summary['claimed']:
        lines.append(f'Unstake finalized: {format_amount(summary["finalized"])} VVV')
    if summary['claimed']:
        lines.append(f'Claimed unstake: {format_amount(summary["claimed"])} VVV')
    if summary['sold']:
        lines.append(f'Sold (verified swap): {format_amount(summary["sold"])} VVV')
    if summary['staked']:
        lines.append(f'Staked: {format_amount(summary["staked"])} VVV')
    if summary['transferred']:
        lines.append(f'Transferred: {format_amount(summary["transferred"])} VVV')
    for source, amount in summary['received'][:3]:
        lines.append(f'Received: {format_amount(amount)} VVV from {short(source)}')
    lines.append(f'Block {logs[0]["block"]:,}')
    lines.append(f'https://basescan.org/tx/{logs[0]["tx"]}')
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--test', action='store_true')
    parser.add_argument('--lookback', type=int, default=0)
    parser.add_argument('--state', default=os.environ.get(
        'UNSTAKER_ALERT_STATE', '.alert-state/state.json'))
    args = parser.parse_args()

    token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
    if not args.dry_run and not token:
        raise SystemExit('TELEGRAM_BOT_TOKEN is required unless --dry-run is used')

    state_path = Path(args.state)
    state = load_state(state_path)
    wallets = tracked_wallets()

    chat_id = os.environ.get('TELEGRAM_CHAT_ID') or state.get('chat_id')
    if not chat_id and token and not args.dry_run:
        chat_id = discover_chat(token)
        if chat_id:
            state['chat_id'] = chat_id
            save_state(state_path, state)

    if args.test:
        if not chat_id:
            raise SystemExit('No chat yet: send /start to the bot, then retry')
        send(token, chat_id, 'VVV unstaker alerts are configured.\nTracking: '
             + ', '.join(short(wallet) for wallet in wallets), args.dry_run)
        return

    rpc = activity.RPC(float(os.environ.get('VVV_ALERT_BUDGET_SECONDS', '240')))
    confirmations = max(0, int(os.environ.get('VVV_ALERT_CONFIRMATIONS', '64')))
    latest = max(0, activity.number(rpc.call('eth_blockNumber', [])) - confirmations)

    cursor = state.get('cursor_block')
    if args.lookback:
        start = max(0, latest - args.lookback)
    elif cursor is None:
        state['cursor_block'] = latest
        save_state(state_path, state)
        if chat_id:
            send(token, chat_id, 'VVV unstaker alerts are active.\nWatching '
                 + ', '.join(short(wallet) for wallet in wallets), args.dry_run)
        print(f'Initialised cursor at block {latest}', flush=True)
        return
    else:
        # Overlap a few blocks so a reorg or a late log is never skipped; the
        # seen set makes the repeat harmless.
        start = max(0, int(cursor) - 4)

    if latest < start:
        print('Chain head has not advanced; nothing to do', flush=True)
        return

    seen = set(state.get('seen', []))
    messages = []
    for wallet in wallets:
        logs = wallet_logs(rpc, wallet, start, latest)
        grouped = {}
        for log in logs:
            grouped.setdefault(log['tx'], []).append(log)
        for tx, tx_logs in grouped.items():
            key = f'{wallet}:{tx}'
            if key in seen:
                continue
            summary = summarize(wallet, tx_logs, rpc)
            if not any(summary[field] for field in
                       ('initiated', 'finalized', 'claimed', 'staked', 'transferred', 'sold', 'received')):
                seen.add(key)
                continue
            messages.append((key, message(wallet, tx_logs, summary)))

    if len(messages) > MESSAGE_LIMIT:
        extra = len(messages) - MESSAGE_LIMIT
        messages = messages[:MESSAGE_LIMIT]
        messages.append((None, f'{extra} more unstaker transactions in this window; '
                               'see https://basescan.org/address/' + wallets[0]))

    for key, text in messages:
        if chat_id or args.dry_run:
            send(token, chat_id, text, args.dry_run)
        else:
            print('No chat configured; skipping send', flush=True)
            print(text, flush=True)
            break
        if key:
            seen.add(key)

    state['seen'] = list(seen)[-SEEN_LIMIT:]
    state['cursor_block'] = latest
    state['updated_at'] = int(time.time())
    save_state(state_path, state)
    print(f'Processed blocks {start}-{latest}; {len(messages)} alert(s)', flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'Unstaker alert run failed: {exc}', flush=True)
        raise SystemExit(2)
