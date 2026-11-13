#!/usr/bin/env python3
"""Post HyperTracker's all-time perp-PnL wallet cohorts to Telegram daily.

The HyperTracker dashboard (app.coinmarketman.com/hypertracker) renders its
"Wallet Cohorts by All Time Perp PNL" section from two public JSON snapshots:
    /segments.json  cohort definitions and aggregate metrics
    /status.json    feed freshness
so no account or API key is required. PNL cohorts are segment ids 8-15.

Usage:
    python scripts/cohort_daily.py --dry-run --force   # print only
    python scripts/cohort_daily.py                     # send inside the window
    python scripts/cohort_daily.py --force             # send now

Environment:
    TELEGRAM_BOT_TOKEN   Bot token from @BotFather.
    TELEGRAM_CHAT_ID     Destination chat (falls back to a stored id).
    COHORT_ALERT_STATE   State file (default .alert-state/cohort.json).
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

AGGREGATOR = 'https://dw3ji7n7thadj.cloudfront.net/aggregator'
PNL_SEGMENTS = range(8, 16)


def nth_sunday(year, month, nth):
    first = datetime(year, month, 1)
    return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (nth - 1))


def chicago_now(now=None):
    """America/Chicago without tzdata, which Windows Python does not ship.

    US DST starts on the second Sunday of March at 02:00 CST (08:00 UTC) and
    ends on the first Sunday of November at 02:00 CDT (07:00 UTC).
    """
    now = now or datetime.now(timezone.utc)
    start = datetime(now.year, 3, nth_sunday(now.year, 3, 2).day, 8, tzinfo=timezone.utc)
    end = datetime(now.year, 11, nth_sunday(now.year, 11, 1).day, 7, tzinfo=timezone.utc)
    daylight = start <= now < end
    offset = timedelta(hours=-5 if daylight else -6)
    return now.astimezone(timezone(offset, 'CDT' if daylight else 'CST'))


def fetch(path):
    url = f'{AGGREGATOR}/{path}'
    last_error = None
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers={'User-Agent': 'vvv-dashboard-cohorts'})
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except Exception as exc:
            last_error = exc
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f'Could not read {url}: {last_error}')


def load_state(path):
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding='utf-8-sig'))


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(state, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def compact(value):
    sign = '-' if value < 0 else ''
    magnitude = abs(value)
    if magnitude >= 1e9:
        body = f'{magnitude / 1e9:g}B'
    elif magnitude >= 1e6:
        body = f'{magnitude / 1e6:g}M'
    elif magnitude >= 1e3:
        body = f'{magnitude / 1e3:g}K'
    else:
        body = f'{magnitude:g}'
    return f'{sign}${body}'


def money(value):
    magnitude = abs(value)
    if magnitude >= 1e9:
        return f'${value / 1e9:.2f}B'
    if magnitude >= 1e6:
        return f'${value / 1e6:.2f}M'
    if magnitude >= 1e3:
        return f'${value / 1e3:.1f}K'
    return f'${value:.0f}'


def pnl_range(criteria):
    low = criteria.get('minPnl')
    high = criteria.get('maxPnl')
    if low is not None and high is not None:
        return f'{compact(low)} to {compact(high)}'
    if low is not None:
        return f'≥ {compact(low)}'
    if high is not None:
        return f'< {compact(high)}'
    return ''


def segment_extra(segment_id):
    """Wallet-count change and profit share, when the per-cohort file loads."""
    try:
        data = fetch(f'segment_{segment_id}.json')
    except Exception:
        return {}
    summary = data.get('openPerpsSummary') or {}
    return {
        'change': (data.get('countTraders', 0) - data.get('countTraders24HoursAgo', 0))
                  if data.get('countTraders24HoursAgo') is not None else None,
        'in_position': summary.get('countTradersInPosition'),
        'in_profit': summary.get('countTradersInProfit'),
    }


def telegram_send(token, chat_id, text):
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


def build_message(segments, status, now):
    """One short line per fact, so nothing wraps on a phone screen."""
    pnl = sorted((item for item in segments
                  if item['segment']['category'] == 'pnl'), key=lambda item: item['segmentId'])
    if not pnl:
        raise RuntimeError('No PNL cohorts in the feed')

    wallets = sum(item['countTraders'] for item in pnl)
    in_position = sum(item['countTradersInPosition'] for item in pnl)
    # Giga-Rekt has only an upper bound, so require an explicit non-negative floor.
    profit = sum(item['countTraders'] for item in pnl
                 if (item['segment']['criteria'].get('minPnl') is not None
                     and item['segment']['criteria']['minPnl'] >= 0))
    open_value = sum(item['positionValue'] for item in pnl)
    equity = sum(item.get('perpMarginEquity') or item.get('perpEquity') or 0 for item in pnl)
    # %-d is not portable to Windows, so assemble the day separately.
    stamp = now.strftime('%a, %b ') + str(now.day) + now.strftime(', %Y %H:%M %Z')
    updated = (status.get('lastUpdate') or '')[:16].replace('T', ' ')
    lines = ['HyperTracker Cohorts',
             'All-time perp PnL',
             stamp,
             f'Feed {updated} UTC' if updated else '',
             '',
             f'{wallets:,} wallets with perp PnL',
             f'{in_position:,} in open positions',
             f'{profit:,} in profit',
             f'{wallets - profit:,} in loss',
             f'{money(open_value)} open',
             f'{money(equity)} equity',
             '']
    for item in pnl:
        segment = item['segment']
        extra = segment_extra(item['segmentId'])
        long_value = item.get('positionLongValue') or 0
        short_value = item.get('positionShortValue') or 0
        long_share = 100 * long_value / (long_value + short_value) if long_value + short_value else 0
        wallet_line = f'{item["countTraders"]:,} wallets'
        if extra.get('change') is not None:
            wallet_line += f' ({extra["change"]:+,} in 24h)'
        lines.append(f'{segment.get("emoji") or ""} {segment["name"]}'.strip())
        lines.append(f'PnL range: {pnl_range(segment["criteria"])}')
        lines.append(wallet_line)
        lines.append(f'{item["countTradersInPosition"]:,} with open positions')
        if extra.get('in_position') and extra.get('in_profit') is not None:
            share = round(100 * extra['in_profit'] / extra['in_position'])
            lines.append(f'{share}% of them in profit')
        lines.append(f'{money(item["positionValue"])} open · {long_share:.0f}% long')
        lines.append('')
    lines.extend(['coinmarketman.com/hypertracker'])
    return '\n'.join(line for line in lines if line is not None)


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--state', default=os.environ.get(
        'COHORT_ALERT_STATE', '.alert-state/cohort.json'))
    args = parser.parse_args()

    now = chicago_now()
    state_path = Path(args.state)
    state = load_state(state_path)
    if not args.force:
        in_window = (now.hour == 8 and now.minute >= 30) or (now.hour == 9 and now.minute < 30)
        if not in_window:
            print(f'Outside the 08:30-09:30 America/Chicago window ({now:%H:%M %Z}); skipping')
            return
        if state.get('last_sent') == now.date().isoformat():
            print('Already sent today; skipping')
            return

    token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
    chat_id = os.environ.get('TELEGRAM_CHAT_ID') or state.get('chat_id')
    if not args.dry_run and (not token or not chat_id):
        raise SystemExit('TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required to send')

    status = fetch('status.json')
    segments = fetch('segments.json').get('segments', [])
    text = build_message(segments, status, now)

    if args.dry_run:
        print(text)
        return
    telegram_send(token, chat_id, text)
    if not args.force:
        state['last_sent'] = now.date().isoformat()
    state['chat_id'] = chat_id
    state['updated_at'] = int(time.time())
    save_state(state_path, state)
    print(f'Sent PNL cohort digest with {len(text)} characters')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'Cohort digest failed: {exc}')
        raise SystemExit(2)
