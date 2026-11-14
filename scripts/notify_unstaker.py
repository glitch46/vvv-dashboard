#!/usr/bin/env python3
"""Telegram/RPC helpers and entry point for the pinned VVV position monitor.

Configuration: data/position_watch.json. Run with --dry-run for read-only
verification, or --test to send a configuration test to TELEGRAM_CHAT_ID.
"""
import json
import os
import urllib.request


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(state, indent=2), encoding='utf-8')
    os.replace(temporary, path)


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


def send(token, chat_id, text, dry_run=False):
    if dry_run:
        print(f'--- message to {chat_id} ---\n{text}\n', flush=True)
        return
    telegram('sendMessage', {'chat_id': chat_id, 'text': text,
                             'disable_web_page_preview': True}, token)


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


def main():
    from position_monitor import main as monitor_main
    monitor_main()


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'Unstaker alert run failed: {exc}', flush=True)
        raise SystemExit(2)
