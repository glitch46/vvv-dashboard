import copy
import datetime as dt
import os
import time
import unittest
from unittest.mock import Mock, patch

import position_monitor as monitor
import unstaker_activity as chain

ROOT = '0x' + '11' * 20
ROOT2 = '0x' + '22' * 20
A = '0x' + '33' * 20
B = '0x' + '44' * 20
C = '0x' + '55' * 20
OTHER = '0x' + '66' * 20


def config():
    return {'roots': {ROOT: 'Original', ROOT2: 'Recipient'}, 'start_block': 10,
            'alert_vvv': '500', 'alert_usd': '10000', 'max_hops': 2,
            'daily_hour_utc': 12, 'seed_transactions': []}


def transfer(src, dst, tokens, block=100, index=0, tx=None, contract=chain.VVV):
    return {'block': block, 'index': index, 'tx': tx or f'0x{block:064x}',
            'address': contract, 'topics': [chain.TRANSFER, chain.topic_address(src), chain.topic_address(dst)],
            'data': hex(int(monitor.Decimal(str(tokens)) * chain.UNIT))}


def event(logs, index=0, sender=None):
    first = logs[0]
    return {'hash': first['tx'], 'block': first['block'], 'index': index,
            'at': 1700000000 + first['block'], 'observed_at': 1700001000,
            'transaction': {'hash': first['tx'], 'from': sender or ROOT,
                            'to': chain.VVV, 'input': '0xa9059cbb', 'value': '0x0'},
            'success': True, 'logs': logs, 'context': {}, 'alerted': False,
            'reported': False, 'quote': None}


def state_with(*events):
    state = monitor.new_state(config(), 99, 1700000000)
    monitor.add_roots(state, config())
    for e in events:
        state['events'][e['hash']] = e
    return state


def recipient(state, wallet, start, depth, parent, opening=0, terminal=False):
    state['wallets'][wallet] = monitor.wallet_entry(
        start, depth, 200, parent, terminal, int(opening * chain.UNIT))
    state['wallets'][wallet]['cursor'] = 200


class ThresholdTests(unittest.TestCase):
    def test_token_and_dollar_thresholds_are_inclusive_or(self):
        self.assertTrue(monitor.meets_threshold(500 * chain.UNIT, None, config()))
        self.assertFalse(monitor.meets_threshold(500 * chain.UNIT - 1, None, config()))
        self.assertTrue(monitor.meets_threshold(400 * chain.UNIT, {'usd': '25'}, config()))
        self.assertFalse(monitor.meets_threshold(400 * chain.UNIT, {'usd': '24.999'}, config()))

    def test_price_outage_does_not_disable_token_alert(self):
        self.assertTrue(monitor.meets_threshold(501 * chain.UNIT, None, config()))
        self.assertFalse(monitor.meets_threshold(chain.UNIT, None, config()))

    def test_price_requires_correct_chain_contract_and_liquidity(self):
        def pair(price, liquidity, token=chain.VVV, network='base'):
            return {'chainId': network, 'baseToken': {'address': token},
                    'pairAddress': OTHER, 'priceUsd': price, 'liquidity': {'usd': liquidity}}
        quote = monitor.select_quote([pair('50', 10), pair('99', 100000000, ROOT),
                                      pair('2', 1000000, network='ethereum'),
                                      pair('20', 200000), pair('21', 300000), pair('NaN', 999999)], 1000)
        self.assertEqual(quote['usd'], '21')
        self.assertEqual(quote['at'], 1000)

    def test_stale_cached_price_is_not_used(self):
        state = {'quote': {'usd': '40', 'at': 1}}
        with patch.object(monitor, 'read_json_url', side_effect=RuntimeError):
            self.assertIsNone(monitor.price_quote(state, 1802))
            self.assertEqual(monitor.price_quote(state, 1801)['usd'], '40')

    def test_real_configuration_is_pinned(self):
        actual = monitor.load_config()
        self.assertEqual(len(actual['roots']), 2)
        self.assertIn('0x1e49568d70acbcad02a08c14ff3abf92efe8aae4', actual['roots'])
        self.assertEqual(actual['alert_vvv'], '500')
        self.assertEqual(actual['max_hops'], 2)
        state = monitor.new_state(actual, 57200000, 1700000000)
        monitor.add_roots(state, actual)
        self.assertEqual(state['wallets']['0x1e49568d70acbcad02a08c14ff3abf92efe8aae4']['start_block'], 57150775)


class TracingTests(unittest.TestCase):
    def test_two_onward_hops_but_not_third_recipient(self):
        state = state_with(event([transfer(ROOT2, A, 1000)]),
                           event([transfer(A, B, 800, block=101)], sender=A),
                           event([transfer(B, C, 700, block=102)], sender=B))
        recipient(state, A, 100, 1, ROOT2)
        recipient(state, B, 101, 2, A)
        found, moves, _, lots = monitor.trace_events(state, config())
        self.assertEqual(found[A]['depth'], 1)
        self.assertEqual(found[B]['depth'], 2)
        self.assertNotIn(C, found)
        self.assertEqual(moves[f'0x{102:064x}'][B], 700 * chain.UNIT)
        self.assertTrue(all(depth is None for _, depth in lots[C]))

    def test_opening_balance_is_consumed_before_linked_funds(self):
        state = state_with(event([transfer(ROOT, A, 1000)]),
                           event([transfer(A, OTHER, 800, block=101)], sender=A),
                           event([transfer(A, OTHER, 800, block=102)], sender=A))
        recipient(state, A, 100, 1, ROOT, opening=1000)
        _, moves, _, _ = monitor.trace_events(state, config())
        self.assertEqual(moves[f'0x{101:064x}'][A], 0)
        self.assertEqual(moves[f'0x{102:064x}'][A], 600 * chain.UNIT)

    def test_small_link_does_not_taint_large_unrelated_balance(self):
        state = state_with(event([transfer(ROOT2, A, 1)]),
                           event([transfer(OTHER, A, 2000, block=101)], sender=OTHER),
                           event([transfer(A, B, 1000, block=102)], sender=A))
        recipient(state, A, 100, 1, ROOT2)
        _, moves, _, _ = monitor.trace_events(state, config())
        self.assertEqual(moves[f'0x{102:064x}'][A], chain.UNIT)
        self.assertFalse(monitor.meets_threshold(moves[f'0x{102:064x}'][A], None, config()))

    def test_zero_value_and_self_transfers_do_not_alert_or_expand(self):
        state = state_with(event([transfer(ROOT, A, 0), transfer(ROOT, ROOT, 1000, index=1)]))
        found, moves, _, _ = monitor.trace_events(state, config())
        self.assertEqual(found, {})
        self.assertEqual(moves[f'0x{100:064x}'], {})

    def test_cycles_do_not_reset_hop_depth(self):
        state = state_with(event([transfer(ROOT2, A, 800)]),
                           event([transfer(A, B, 800, block=101)], sender=A),
                           event([transfer(B, A, 800, block=102)], sender=B),
                           event([transfer(A, C, 800, block=103)], sender=A))
        recipient(state, A, 100, 1, ROOT2)
        recipient(state, B, 101, 2, A)
        found, moves, _, _ = monitor.trace_events(state, config())
        self.assertNotIn(C, found)
        self.assertEqual(moves[f'0x{103:064x}'][A], 0)

    def test_contracts_are_terminal_not_unlimited_pool_tracing(self):
        state = state_with(event([transfer(ROOT2, A, 800), transfer(A, B, 800, index=1)]))
        recipient(state, A, 100, 1, ROOT2, terminal=True)
        found, _, _, _ = monitor.trace_events(state, config())
        self.assertNotIn(B, found)

    def test_recipient_is_not_attributed_until_history_is_contiguous(self):
        state = state_with(event([transfer(ROOT2, A, 1000, block=90)]),
                           event([transfer(A, B, 800, block=101)], sender=A))
        recipient(state, A, 90, 1, ROOT2)
        state['wallets'][A]['cursor'] = 89
        _, moves, _, _ = monitor.trace_events(state, config())
        self.assertNotIn(A, moves[f'0x{101:064x}'])

    def test_earlier_backfill_link_expands_existing_recipient_coverage(self):
        state = state_with(event([transfer(ROOT2, A, 1000, block=50)]))
        recipient(state, A, 100, 1, ROOT2)
        rpc = Mock()
        rpc.call.return_value = hex(5 * chain.UNIT)
        monitor.expand_wallets(state, config(), rpc)
        self.assertEqual(state['wallets'][A]['start_block'], 50)
        self.assertEqual(state['wallets'][A]['cursor'], 49)
        self.assertEqual(state['wallets'][A]['opening_wei'], 5 * chain.UNIT)

    def test_verified_claim_preserves_linked_restake(self):
        claim = transfer(chain.STAKING, A, 600, block=102)
        claim_event = event([claim], sender=A)
        claim_event['transaction'].update(to=chain.STAKING, input='0x0ea94341')
        state = state_with(event([transfer(ROOT, A, 600)]),
                           event([transfer(A, chain.STAKING, 600, block=101)], sender=A),
                           claim_event, event([transfer(A, B, 600, block=103)], sender=A))
        recipient(state, A, 100, 1, ROOT)
        _, moves, _, _ = monitor.trace_events(state, config())
        self.assertEqual(moves[f'0x{103:064x}'][A], 600 * chain.UNIT)


class DeliveryTests(unittest.TestCase):
    def test_root_to_root_has_one_alert_not_two(self):
        state = state_with(event([transfer(ROOT, ROOT2, 600)]))
        rpc = Mock()
        rpc.call.return_value = hex(600 * chain.UNIT)
        with patch.object(chain, 'verified_swap_amount', return_value=0):
            monitor.queue_alerts(state, config(), rpc, None, 1000)
            monitor.queue_alerts(state, config(), rpc, None, 1100)
        self.assertEqual(len(state['outbox']), 1)
        self.assertIn('600 VVV', state['outbox'][0]['text'])

    def test_subthreshold_and_approvals_report_without_alert(self):
        approval = {'block': 100, 'index': 1, 'tx': f'0x{100:064x}', 'address': chain.VVV,
                    'topics': [monitor.APPROVAL, chain.topic_address(ROOT), chain.topic_address(A)],
                    'data': hex(1000000 * chain.UNIT)}
        state = state_with(event([transfer(ROOT, ROOT2, 1), approval]))
        rpc = Mock()
        monitor.queue_alerts(state, config(), rpc, {'usd': '20'}, 1000)
        self.assertEqual(state['outbox'], [])
        description = '\n'.join(monitor.event_description(next(iter(state['events'].values())), state['wallets']))
        self.assertIn('Approval:', description)
        self.assertIn('1 VVV', description)

    def test_historical_baseline_does_not_trigger_old_alerts(self):
        state = state_with(event([transfer(ROOT, ROOT2, 400000, block=90)]))
        monitor.queue_alerts(state, config(), Mock(), {'usd': '20'}, 1000)
        self.assertEqual(state['outbox'], [])

    def test_missing_price_can_be_retried_without_dropping_event(self):
        state = state_with(event([transfer(ROOT, ROOT2, 400)]))
        rpc = Mock()
        rpc.call.return_value = '0x0'
        monitor.queue_alerts(state, config(), rpc, None, 1000)
        self.assertEqual(state['outbox'], [])
        with patch.object(chain, 'verified_swap_amount', return_value=0):
            monitor.queue_alerts(state, config(), rpc, {'usd': '25', 'at': 1001, 'source': 'fixture'}, 1001)
        self.assertEqual(len(state['outbox']), 1)
        self.assertIn('$10,000.00', state['outbox'][0]['text'])

    def test_twenty_message_delivery_limit_preserves_remaining_messages(self):
        state = {'outbox': [{'id': str(i), 'text': str(i)} for i in range(25)]}
        saved = []
        with patch.object(monitor.delivery, 'send') as send, patch.object(monitor.time, 'sleep'):
            monitor.flush(state, lambda: saved.append(copy.deepcopy(state)), 'token', 'chat')
        self.assertEqual(send.call_count, 20)
        self.assertEqual([item['id'] for item in state['outbox']], [str(i) for i in range(20, 25)])
        self.assertEqual(len(saved), 20)

    def test_telegram_failure_keeps_failed_and_unsent_pages(self):
        state = {'outbox': [{'id': str(i), 'text': str(i)} for i in range(3)]}
        with patch.object(monitor.delivery, 'send', side_effect=[None, RuntimeError('offline')]), patch.object(monitor.time, 'sleep'):
            with self.assertRaises(RuntimeError):
                monitor.flush(state, Mock(), 'token', 'chat')
        self.assertEqual([item['id'] for item in state['outbox']], ['1', '2'])

    def test_long_reports_are_paged_without_truncation(self):
        lines = [f'line {i}: ' + 'x' * 300 for i in range(100)]
        pages = monitor.chunks(lines)
        self.assertGreater(len(pages), 1)
        self.assertTrue(all(len(page) <= 3600 for page in pages))
        self.assertEqual('\n'.join(pages), '\n'.join(lines))

    def test_movement_alerts_take_priority_over_daily_report_backlog(self):
        state = {'outbox': []}
        monitor.queue(state, 'daily:one', ['daily', 'older report'])
        monitor.queue(state, 'alert:one', ['alert', 'new movement'])
        monitor.queue(state, 'alert:two', ['alert', 'second movement'])
        self.assertEqual([item['id'] for item in state['outbox']],
                         ['alert:one:0', 'alert:two:0', 'daily:one:0'])

    def test_daily_schedule_uses_noon_utc_independent_of_poll_count(self):
        def timestamp(hour):
            return int(dt.datetime(2026, 11, 14, hour, tzinfo=dt.timezone.utc).timestamp())
        self.assertEqual(monitor.daily_key(timestamp(11), 12), '2026-11-13')
        self.assertEqual(monitor.daily_key(timestamp(12), 12), '2026-11-14')
        self.assertEqual(monitor.daily_key(timestamp(23), 12), '2026-11-14')

    def test_staking_reward_is_not_labeled_unstake_claim(self):
        e = event([transfer(chain.STAKING, ROOT, 600)])
        text = '\n'.join(monitor.event_description(e, {ROOT: {}}))
        self.assertIn('Staking reward/other payment', text)
        self.assertNotIn('Verified unstake claim', text)


class ReceiptTests(unittest.TestCase):
    def rpc(self, normalized):
        tx_hash = normalized['tx']
        raw_log = {'address': normalized['address'], 'topics': normalized['topics'], 'data': normalized['data'],
                   'blockNumber': hex(normalized['block']), 'logIndex': hex(normalized['index']),
                   'transactionHash': tx_hash}
        rpc = Mock()
        rpc.call.return_value = {'transactionHash': tx_hash, 'blockHash': '0xabc',
                                 'blockNumber': hex(normalized['block']), 'status': '0x1', 'logs': [raw_log]}
        rpc.transaction.return_value = {'hash': tx_hash, 'blockHash': '0xabc', 'blockNumber': hex(normalized['block']),
                                        'transactionIndex': '0x0', 'from': ROOT, 'to': chain.VVV,
                                        'input': '0xa9059cbb', 'value': '0x0'}
        rpc.timestamp.return_value = 1700000000
        return rpc

    def test_discovery_amount_must_match_receipt(self):
        log = transfer(ROOT, ROOT2, 600)
        rpc = self.rpc(log)
        forged = {**log, 'data': hex(100000 * chain.UNIT)}
        with self.assertRaisesRegex(RuntimeError, 'does not match'):
            monitor.ingest(state_with(), rpc, log['tx'], 1700001000, [forged])

    def test_mismatched_receipt_block_is_rejected(self):
        log = transfer(ROOT, ROOT2, 600)
        rpc = self.rpc(log)
        rpc.transaction.return_value['blockNumber'] = '0x1'
        with self.assertRaisesRegex(RuntimeError, 'Mismatched'):
            monitor.ingest(state_with(), rpc, log['tx'], 1700001000, [log])

    def test_repeated_receipt_is_deduplicated(self):
        log = transfer(ROOT, ROOT2, 600)
        rpc = self.rpc(log)
        state = state_with()
        monitor.ingest(state, rpc, log['tx'], 1700001000, [log])
        monitor.ingest(state, rpc, log['tx'], 1700001001, [log])
        self.assertEqual(len(state['events']), 1)
        self.assertEqual(rpc.call.call_count, 1)


class PollingTests(unittest.TestCase):
    def rpc(self):
        rpc = Mock()
        rpc.chunk = 40
        rpc.deadline = time.monotonic() + 360
        rpc.index_provider = None
        rpc.call.return_value = '0x0'
        rpc.is_contract.return_value = False
        return rpc

    def test_new_recipient_and_second_hop_are_scanned_in_same_poll(self):
        events = [event([transfer(ROOT2, A, 1000)]),
                  event([transfer(A, B, 800, block=101)], sender=A),
                  event([transfer(B, C, 700, block=102)], sender=B)]
        state = state_with()
        for info in state['wallets'].values():
            info['live_cursor'] = 99

        def scan(target, rpc, wallet, start, end, now):
            for e in events:
                if start <= e['block'] <= end and any(
                        wallet in (move['from'], move['to']) for move in chain.transfers(e['logs'])):
                    target['events'].setdefault(e['hash'], copy.deepcopy(e))

        with patch.object(monitor, 'scan_core', side_effect=scan), \
                patch.object(monitor, 'refresh_labels'), \
                patch.object(monitor, 'report_lines', return_value=(['report'], [], True)):
            monitor.run(state, config(), self.rpc(), 102, 1700001000, Mock(), None)
        self.assertEqual(state['wallets'][A]['live_cursor'], 102)
        self.assertEqual(state['wallets'][B]['live_cursor'], 102)
        self.assertNotIn(C, state['wallets'])
        self.assertEqual(len([item for item in state['outbox'] if item['id'].startswith('alert:')]), 3)

    def test_failed_window_does_not_advance_live_checkpoint(self):
        state = state_with()
        for info in state['wallets'].values():
            info['live_cursor'] = 99
        with patch.object(monitor, 'scan_core', side_effect=RuntimeError('RPC offline')), \
                patch.object(monitor, 'refresh_labels'):
            with self.assertRaisesRegex(RuntimeError, 'RPC offline'):
                monitor.run(state, config(), self.rpc(), 102, 1700001000, Mock(), None)
        self.assertEqual(state['wallets'][ROOT]['live_cursor'], 99)

    def test_balance_reconciliation_retries_missing_history(self):
        state = state_with()
        for info in state['wallets'].values():
            info.update(cursor=100, live_cursor=100)
        rpc = self.rpc()
        rpc.timestamp.return_value = 1700001000
        rpc.call.return_value = hex(1000 * chain.UNIT)
        balances = {ROOT: {'liquid': 400 * chain.UNIT, 'staked': 0, 'pending': 0, 'cooldown_end': 0}}
        with patch.object(monitor, 'snapshot', return_value=balances):
            lines, _, reconciled = monitor.report_lines(state, config(), rpc, 100, 1700001000, True)
        self.assertFalse(reconciled)
        self.assertEqual(state['wallets'][ROOT]['cursor'], 9)
        self.assertIn('Balance reconciliation incomplete', '\n'.join(lines))

    def test_balanced_history_is_verified(self):
        state = state_with(event([transfer(ROOT, ROOT2, 600)]))
        for info in state['wallets'].values():
            info.update(cursor=100, live_cursor=100)
        rpc = self.rpc()
        rpc.timestamp.return_value = 1700001000
        rpc.call.return_value = hex(1000 * chain.UNIT)
        balances = {ROOT: {'liquid': 400 * chain.UNIT, 'staked': 0, 'pending': 0, 'cooldown_end': 0}}
        with patch.object(monitor, 'snapshot', return_value=balances):
            lines, _, reconciled = monitor.report_lines(state, config(), rpc, 100, 1700001000, True)
        self.assertTrue(reconciled)
        self.assertIn('Balance reconciliation verified', '\n'.join(lines))

    def test_dry_run_cannot_write_state_or_call_telegram(self):
        rpc = self.rpc()
        rpc.call.return_value = '0x100'
        with patch('sys.argv', ['notify_unstaker.py', '--dry-run']), \
                patch.object(monitor, 'load_config', return_value=config()), \
                patch.object(monitor.Path, 'exists', return_value=False), \
                patch.object(monitor, 'PositionRPC', return_value=rpc), \
                patch.object(monitor, 'price_quote', return_value=None), \
                patch.object(monitor, 'run'), \
                patch.object(monitor.delivery, 'save_state') as save, \
                patch.object(monitor.delivery, 'telegram') as telegram:
            monitor.main()
        save.assert_not_called()
        telegram.assert_not_called()


class ExchangeAttributionTests(unittest.TestCase):
    def test_changed_sourced_evidence_reports_once_without_waiting_for_daily(self):
        settings = config()
        settings['address_labels'] = {A: {'label': 'ByBit Deposit', 'source': 'https://etherscan.io/address/' + A}}
        state = state_with()
        now = 1700001000
        state.update(baseline_queued=True, last_daily=monitor.daily_key(now, 12))
        for info in state['wallets'].values():
            info.update(cursor=99, live_cursor=99)
        rpc = PollingTests().rpc()
        with patch.object(monitor, 'refresh_labels'), \
                patch.object(monitor, 'report_lines', return_value=(['report'], [], True)) as report:
            monitor.run(state, settings, rpc, 99, now, Mock(), None)
            monitor.run(state, settings, rpc, 99, now + 600, Mock(), None)
            self.assertEqual(report.call_count, 1)
            self.assertEqual(len(state['outbox']), 1)
            self.assertTrue(state['outbox'][0]['id'].startswith('attribution:'))
            settings['address_labels'][A]['label'] = 'Bybit: Hot Wallet'
            monitor.run(state, settings, rpc, 99, now + 1200, Mock(), None)
            self.assertEqual(report.call_count, 2)
            self.assertEqual(len(state['outbox']), 2)

    def test_public_deposit_tag_is_sourced_not_called_proven_ownership(self):
        label = monitor.explorer_label(
            f'ByBit Deposit: {A} | Address: 0x3333...3333 | Etherscan', A, 1700000000)
        self.assertEqual(label['entity'].lower(), 'bybit')
        self.assertEqual(label['confidence'], 'explorer-tagged')
        self.assertEqual(label['source'], f'https://etherscan.io/address/{A}')
        self.assertFalse(label['terminal'])

    def test_unlabeled_address_is_not_classified_as_private_or_exchange(self):
        label = monitor.explorer_label('Address: 0x1111...1111 | Etherscan', ROOT, 1700000000)
        self.assertIsNone(label['entity'])
        self.assertEqual(label['confidence'], 'unverified')

    def test_sending_to_bybit_does_not_propagate_ownership_label_to_sender(self):
        state = state_with(event([transfer(ROOT, A, 1)]))
        recipient(state, A, 100, 1, ROOT)
        settings = config()
        settings['address_labels'] = {A: monitor.explorer_label('ByBit Deposit | Address: 0x3333 | Etherscan', A, 1700000000)}
        with patch.object(monitor.urllib.request, 'urlopen', side_effect=RuntimeError('unavailable')):
            monitor.refresh_labels(state, settings, 1700000000)
        self.assertEqual(state['labels'][A]['entity'].lower(), 'bybit')
        self.assertIsNone(state['labels'][ROOT]['entity'])
        text = '\n'.join(monitor.event_description(next(iter(state['events'].values())), state['wallets'], labels=state['labels']))
        self.assertIn('ByBit Deposit', text)
        self.assertIn('not verified sale', text)

    def test_source_checked_hot_wallet_is_terminal(self):
        state = state_with()
        recipient(state, A, 100, 1, ROOT)
        settings = config()
        settings['address_labels'] = {A: monitor.explorer_label('Bybit: Hot Wallet 6 | Address: 0x3333 | Etherscan', A, 1700000000)}
        with patch.object(monitor.urllib.request, 'urlopen', side_effect=RuntimeError('unavailable')):
            monitor.refresh_labels(state, settings, 1700000000)
        self.assertTrue(state['wallets'][A]['terminal'])

    def test_public_rpc_precedes_authenticated_logs_without_losing_index(self):
        with patch.dict(os.environ, {'ALCHEMY_API_KEY': 'test', 'VVV_ACTIVITY_RPC_PROVIDER': 'alchemy'}):
            rpc = monitor.PositionRPC(10)
        self.assertNotEqual(rpc.providers[0], rpc.index_provider)
        self.assertIn('publicnode', rpc.providers[0][0])
        self.assertIsNotNone(rpc.index_provider)


if __name__ == '__main__':
    unittest.main()
