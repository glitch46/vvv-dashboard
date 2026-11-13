#!/usr/bin/env python3
"""Alert when BTC tests its week-anchored VWAP from below inside 72k-73k.

Rules (fixed by the dashboard owner):
    * VWAP resets Monday 00:00 UTC and accumulates hourly candles.
    * Spot must be 72,000-73,000 and no more than 0.5% below that VWAP,
      i.e. price pressing up into weekly VWAP resistance.
    * One alert per entry. It re-arms after price breaks above the VWAP or
      falls 1.5% below it.

Prices come from Coinbase BTC-USD, with Kraken XBT/USD as a fallback, because
Binance blocks some US-hosted runners.

Usage:
    python scripts/btc_vwap_alert.py --dry-run
    python scripts/btc_vwap_alert.py            # send if qualifying
    python scripts/btc_vwap_alert.py --test     # send a sample message
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

LOW = 72000.0
HIGH = 73000.0
TOLERANCE = 0.005
REARM = 0.015
MIN_CANDLES = 12


def fetch_json(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'vvv-dashboard-btc'})
    with urllib.request.urlopen(request, timeout=25) as response:
        return json.load(response)


def coinbase():
    ticker = fetch_json('https://api.exchange.coinbase.com/products/BTC-USD/ticker')
    candles = fetch_json('https://api.exchange.coinbase.com/products/BTC-USD/candles'
                         '?granularity=3600')
    rows = [{'time': row[0], 'high': row[2], 'low': row[1], 'close': row[4], 'volume': row[5]}
            for row in candles]
    return float(ticker['price']), rows, 'Coinbase BTC-USD'


def kraken():
    ticker = fetch_json('https://api.kraken.com/0/public/Ticker?pair=XBTUSD')
    ohlc = fetch_json('https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval=60')
    pair = next(iter(ohlc['result']))
    rows = [{'time': int(row[0]), 'high': float(row[2]), 'low': float(row[3]),
             'close': float(row[4]), 'volume': float(row[6])}
            for row in ohlc['result'][pair]]
    return float(ticker['result'][pair]['c'][0]), rows, 'Kraken XBT/USD'


def market_data():
    errors = []
    for source in (coinbase, kraken):
        try:
            return source()
        except Exception as exc:
            errors.append(f'{source.__name__}: {exc}')
    raise RuntimeError('No BTC price source available: ' + '; '.join(errors))


def week_vwap(rows, now):
    start = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)
    current = [row for row in rows if row['time'] >= start.timestamp()]
    volume = sum(row['volume'] for row in current)
    if len(current) < MIN_CANDLES or volume <= 0:
        return None, len(current), start
    typical = sum((row['high'] + row['low'] + row['close']) / 3 * row['volume'] for row in current)
    return typical / volume, len(current), start


def qualifies(spot, vwap):
    """Spot inside 72k-73k and no more than 0.5% below the VWAP."""
    gap = (vwap - spot) / vwap
    return (LOW <= spot <= HIGH and 0 <= gap <= TOLERANCE), gap


def load_state(path):
    if not path.exists():
        return {'armed': True}
    return json.loads(path.read_text(encoding='utf-8-sig'))


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(state, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def send(token, chat_id, text):
    payload = json.dumps({'chat_id': chat_id, 'text': text,
                          'disable_web_page_preview': True}).encode()
    request = urllib.request.Request(
        f'https://api.telegram.org/bot{token}/sendMessage', data=payload,
        headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
    except Exception as exc:
        raise RuntimeError(f'Telegram send failed: {exc}') from exc
    if not result.get('ok'):
        raise RuntimeError(f'Telegram send failed: {result.get("description")}')


def message(spot, vwap, source, stamp):
    gap = (vwap - spot) / vwap * 100
    return '\n'.join([
        'BTC at weekly VWAP resistance',
        '',
        f'Spot: ${spot:,.0f}',
        f'Weekly VWAP: ${vwap:,.0f}',
        f'Spot is {gap:.2f}% below VWAP',
        f'Band: $72,000-$73,000',
        '',
        'VWAP anchored Monday 00:00 UTC',
        f'Source: {source}',
        stamp])


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--test', action='store_true')
    parser.add_argument('--state', default=os.environ.get(
        'BTC_VWAP_STATE', '.alert-state/btc-vwap.json'))
    args = parser.parse_args()

    token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
    chat_id = os.environ.get('TELEGRAM_CHAT_ID', '').strip()
    if not args.dry_run and (not token or not chat_id):
        raise SystemExit('TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required')

    now = datetime.now(timezone.utc)
    stamp = ('Checked ' + now.strftime('%a, %b ') + str(now.day)
             + now.strftime(', %Y %H:%M UTC'))
    spot, rows, source = market_data()
    vwap, count, start = week_vwap(rows, now)

    if vwap is None:
        print(f'Only {count} hourly candles since {start:%Y-%m-%d %H:%M} UTC; '
              f'need {MIN_CANDLES} before the VWAP is usable.')
        return

    qualifying, gap = qualifies(spot, vwap)
    state_path = Path(args.state)
    state = load_state(state_path)
    armed = state.get('armed', True)

    print(f'spot ${spot:,.0f} vwap ${vwap:,.0f} gap {gap * 100:+.2f}% '
          f'candles {count} armed {armed} qualifying {qualifying}')

    if args.test:
        text = message(spot, vwap, source, stamp)
        if args.dry_run:
            print(text)
        else:
            send(token, chat_id, '[Test]\n' + text)
            print('Sent test message')
        return

    if qualifying and armed:
        text = message(spot, vwap, source, stamp)
        if args.dry_run:
            print(text)
            print('Would send alert')
        else:
            send(token, chat_id, text)
            print('Sent VWAP resistance alert')
        state['armed'] = False
        state['alerted_at'] = now.isoformat()
    elif not qualifying and (gap < 0 or gap > REARM):
        # Re-arm once price closes above the VWAP or pulls 1.5% below it.
        if not armed:
            print('Re-armed')
        state['armed'] = True

    state.update({'spot': spot, 'vwap': vwap, 'candles': count,
                  'updated_at': now.isoformat(), 'source': source})
    if not args.dry_run:
        save_state(state_path, state)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'BTC VWAP alert failed: {exc}')
        raise SystemExit(2)
