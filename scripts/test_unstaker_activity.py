"""Accounting and evidence regression tests; no live RPC or secret required."""
import copy
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import unstaker_activity as activity

WALLET = '0x' + '1' * 40
OTHER = '0x' + '2' * 40
POOL = '0x' + '3' * 40
QUOTE = '0x' + '4' * 40
ROUTER = '0x' + '5' * 40


def abi(*values):
    return '0x' + ''.join(f'{value % (2 ** 256):064x}' for value in values)


def transfer(src, dst, amount, tx='0xclaim', block=1, index=1, token=activity.VVV):
    return {'address': token, 'block': block, 'index': index, 'tx': tx,
            'topics': [activity.TRANSFER, activity.topic_address(src), activity.topic_address(dst)],
            'data': abi(amount * activity.UNIT)}


class EvidenceTests(unittest.TestCase):
    def test_initiation_reward_is_not_an_unstake_claim(self):
        logs = [transfer(activity.STAKING, WALLET, 112)]
        tx = {'to': activity.STAKING, 'from': WALLET, 'input': '0x12345678'}
        self.assertEqual(activity.verified_claim_amount(WALLET, '0xclaim', logs, tx), 0)

    def test_finalization_event_caps_reward_plus_claim_transfer(self):
        logs = [transfer(activity.STAKING, WALLET, 120), {
            'address': activity.STAKING, 'topics': [activity.FINALIZED, activity.topic_address(WALLET)],
            'data': abi(100 * activity.UNIT)}]
        self.assertEqual(activity.verified_claim_amount(WALLET, '0xclaim', logs, {}), 100 * activity.UNIT)
        logs[-1]['topics'][1] = activity.topic_address(OTHER)
        self.assertEqual(activity.verified_claim_amount(WALLET, '0xclaim', logs, {}), 0)

    def test_direct_finalization_call_is_evidence(self):
        logs = [transfer(activity.STAKING, WALLET, 100)]
        self.assertEqual(activity.verified_claim_amount(WALLET, '0xclaim', logs,
            {'to': activity.STAKING, 'from': WALLET, 'input': '0x0ea94341'}), 100 * activity.UNIT)

    def swap_fixture(self, signature=activity.SWAP_V2):
        values = (50 * activity.UNIT, 0, 0, 25 * activity.UNIT) if signature == activity.SWAP_V2 else (
            50 * activity.UNIT, -25 * activity.UNIT, 1, 1, 1)
        return [transfer(WALLET, ROUTER, 50), transfer(ROUTER, POOL, 50, index=2),
                transfer(POOL, ROUTER, 25, token=QUOTE, index=3),
                transfer(ROUTER, WALLET, 25, token=QUOTE, index=4),
                {'address': POOL, 'block': 1, 'index': 5, 'topics': [signature], 'data': abi(*values)}]

    def test_verified_v2_and_v3_swaps_through_router(self):
        for signature in (activity.SWAP_V2, activity.SWAP_V3, activity.SWAP_SLIPSTREAM):
            with self.subTest(signature=signature):
                logs = self.swap_fixture(signature)
                amount = activity.verified_swap_amount(WALLET, logs, {'from': WALLET},
                                                       lambda pool, block: (activity.VVV, QUOTE))
                self.assertEqual(amount, 50 * activity.UNIT)

    def test_transfer_to_pool_without_swap_is_not_sold(self):
        logs = self.swap_fixture()[:-1]
        self.assertEqual(activity.verified_swap_amount(WALLET, logs, {'from': WALLET}, Mock()), 0)

    def test_liquidity_buy_and_unrelated_quote_receipt_are_not_sales(self):
        logs = self.swap_fixture()
        logs[-1]['data'] = abi(0, 25 * activity.UNIT, 50 * activity.UNIT, 0)  # VVV bought
        self.assertEqual(activity.verified_swap_amount(WALLET, logs, {'from': WALLET},
                         lambda *args: (activity.VVV, QUOTE)), 0)
        logs = self.swap_fixture()
        logs[2] = transfer(OTHER, ROUTER, 25, token=QUOTE)  # unrelated output
        self.assertEqual(activity.verified_swap_amount(WALLET, logs, {'from': WALLET},
                         lambda *args: (activity.VVV, QUOTE)), 0)
        self.assertEqual(activity.verified_swap_amount(WALLET, self.swap_fixture(), {'from': OTHER}, Mock()), 0)


class AttributionTests(unittest.TestCase):
    def analyze(self, logs, opening=0, claims=None, sales=None, timestamps=None, contracts=None):
        return activity.analyze_wallet(WALLET, logs, opening * activity.UNIT,
            {tx: amount * activity.UNIT for tx, amount in (claims or {'0xclaim': 100}).items()},
            {tx: amount * activity.UNIT for tx, amount in (sales or {}).items()}, contracts or set(),
            timestamps or {'1': 1000}, 1000 + 90 * activity.DAY, 1000)

    def test_unrelated_opening_balance_consumed_before_claim(self):
        logs = [transfer(activity.STAKING, WALLET, 100),
                transfer(WALLET, OTHER, 120, tx='0xsale', block=2)]
        result = self.analyze(logs, opening=100, sales={'0xsale': 120}, timestamps={'1': 1000, '2': 1001})
        self.assertEqual(result['series']['sold'][0], 20)
        self.assertEqual(result['series']['held'][-1], 80)
        self.assertEqual(sum(sum(values) for values in result['series'].values()), 100)

    def test_restake_and_wallet_transfer_are_distinct_from_sale(self):
        logs = [transfer(activity.STAKING, WALLET, 100),
                transfer(WALLET, activity.STAKING, 30, tx='0xstake', block=2),
                transfer(WALLET, OTHER, 20, tx='0xmove', block=3)]
        result = self.analyze(logs, timestamps={'1': 1000, '2': 1000 + activity.DAY, '3': 1000 + 3 * activity.DAY})
        self.assertEqual(sum(result['series']['sold']), 0)
        self.assertEqual(result['series']['restaked'][1], 30)
        self.assertEqual(result['series']['transferred'][3], 20)
        self.assertEqual(result['series']['held'][-1], 50)

    def test_contract_transfer_and_self_transfer(self):
        logs = [transfer(activity.STAKING, WALLET, 100), transfer(WALLET, WALLET, 50, tx='0xself'),
                transfer(WALLET, OTHER, 10, tx='0xmove', index=2)]
        result = self.analyze(logs, contracts={OTHER})
        self.assertEqual(result['series']['other_transfer'][0], 10)
        self.assertEqual(result['series']['held'][-1], 90)

    def test_refund_is_not_new_income_or_extra_outflow(self):
        logs = [transfer(activity.STAKING, WALLET, 100),
                transfer(WALLET, ROUTER, 100, tx='0xswap', block=2),
                transfer(ROUTER, WALLET, 20, tx='0xswap', block=2, index=2)]
        result = self.analyze(logs, sales={'0xswap': 80}, timestamps={'1': 1000, '2': 1001})
        self.assertEqual(result['series']['sold'][0], 80)
        self.assertEqual(result['series']['held'][-1], 20)

    def test_no_verified_claims_does_not_turn_rewards_into_sales(self):
        logs = [transfer(activity.STAKING, WALLET, 100), transfer(WALLET, OTHER, 100, index=2)]
        result = self.analyze(logs, claims={'0xclaim': 0}, sales={'0xclaim': 100})
        self.assertEqual(result['claim_count'], 0)
        self.assertEqual(sum(sum(values) for values in result['series'].values()), 0)

    def test_exact_non_overlapping_bucket_boundaries(self):
        self.assertEqual([activity.age_bucket(t) for t in [0, activity.DAY - 1, activity.DAY,
            2 * activity.DAY, 3 * activity.DAY, 7 * activity.DAY, 90 * activity.DAY]], [0, 0, 1, 2, 3, 4, 4])

    def test_partial_sale_does_not_double_count_and_preserves_claim_conservation(self):
        logs = [transfer(activity.STAKING, WALLET, 100),
                transfer(WALLET, ROUTER, 80, tx='0xswap', block=2),
                transfer(WALLET, OTHER, 20, tx='0xmove', block=3)]
        result = self.analyze(logs, sales={'0xswap': 50}, contracts={ROUTER},
                              timestamps={'1': 1000, '2': 1001, '3': 1002})
        self.assertEqual(sum(result['series']['sold']), 50)
        self.assertEqual(sum(result['series']['other_transfer']), 30)
        self.assertEqual(sum(result['series']['transferred']), 20)
        self.assertEqual(result['balance_wei'], 0)
        self.assertEqual(sum(sum(values) for values in result['series'].values()), 100)


class IndexingTests(unittest.TestCase):
    def test_dynamic_top_seven_excludes_older_initiations(self):
        now = 10 * activity.DAY
        logs = [{'address': activity.STAKING, 'block': index, 'topics': [activity.INITIATED,
                 activity.topic_address('0x' + f'{index:040x}')], 'data': abi(index * activity.UNIT)}
                for index in range(1, 10)]
        selected, totals = activity.rank_wallets(logs, lambda block: now if block != 9 else now - 7 * activity.DAY - 1, now)
        self.assertEqual(len(selected), 7)
        self.assertTrue(selected[0].endswith('8'))
        self.assertNotIn('0x' + f'{9:040x}', totals)

    def test_exact_ranking_block_boundaries_need_no_per_event_rpc(self):
        log = {'address': activity.STAKING, 'block': 100,
               'topics': [activity.INITIATED, activity.topic_address(WALLET)], 'data': abi(activity.UNIT)}
        timestamp = Mock(side_effect=AssertionError('Unnecessary timestamp RPC'))
        selected, totals = activity.rank_wallets([log, {**log, 'block': 99}], timestamp, 999999, (100, 110))
        self.assertEqual(selected, [WALLET])
        self.assertEqual(totals[WALLET]['count'], 1)

    def test_completed_scan_resumes_without_duplicate_log_queries(self):
        rpc = Mock()
        rpc.chunk = 10000
        log = transfer(activity.STAKING, WALLET, 100, block=10)
        rpc.logs.return_value = [log]
        state = {}
        result = activity.scan(rpc, state, 'history', activity.VVV, [[activity.TRANSFER]], 10, 20, Mock())
        self.assertEqual(result, [log])
        self.assertEqual(state['history']['next_block'], 21)
        rpc.logs.reset_mock()
        activity.scan(rpc, state, 'history', activity.VVV, [[activity.TRANSFER]], 10, 20, Mock())
        rpc.logs.assert_not_called()

    def test_failed_second_direction_does_not_checkpoint_partial_chunk(self):
        rpc = Mock()
        rpc.chunk = 10000
        rpc.logs.side_effect = [[], RuntimeError('network down')]
        state = {}
        save = Mock()
        with self.assertRaises(RuntimeError):
            activity.scan(rpc, state, 'history', activity.VVV, [[activity.TRANSFER], [activity.TRANSFER]], 10, 20, save)
        self.assertEqual(state['history']['next_block'], 10)
        self.assertEqual(state['history']['logs'], [])
        save.assert_not_called()


class PublicationTests(unittest.TestCase):
    def fake_rpc(self):
        rpc = Mock()
        rpc.chunk = 10000
        now = 100 * activity.DAY
        def call(method, params):
            if method == 'eth_call':
                return hex(0 if params[-1] == hex(99) else 80 * activity.UNIT)
            return {'eth_chainId': hex(8453), 'eth_blockNumber': hex(1064)}[method]
        rpc.call.side_effect = call
        rpc.timestamp.side_effect = lambda block: now if block == 1000 else now - (2 if block == 950 else 1) * activity.DAY
        rpc.first_block.side_effect = [900, 100]
        claim = transfer(activity.STAKING, WALLET, 100, block=950)
        moved = transfer(WALLET, OTHER, 20, tx='0xmove', block=960)
        rank = {'address': activity.STAKING, 'block': 990, 'index': 1, 'tx': '0xinit',
                'topics': [activity.INITIATED, activity.topic_address(WALLET)], 'data': abi(500 * activity.UNIT)}
        rpc.logs.side_effect = lambda address, topics, start, end: ([rank] if address == activity.STAKING else
            [claim] if topics[1] is None else [moved])
        rpc.receipt.side_effect = lambda tx: [claim] if tx == '0xclaim' else [moved]
        rpc.transaction.return_value = {'from': WALLET, 'to': activity.STAKING, 'input': '0x0ea94341'}
        rpc.is_contract.return_value = False
        return rpc

    def run_main(self, rpc, memory):
        def save(path, value):
            memory[path.name] = copy.deepcopy(value)
        with patch.object(activity, 'DATA', Path('/memory')), patch.object(activity, 'RPC', return_value=rpc), \
                patch.object(activity, 'atomic_json', side_effect=save), \
                patch.object(activity, 'load_json', side_effect=lambda path, default: copy.deepcopy(memory.get(path.name, default))):
            activity.main()

    def test_complete_snapshot_with_reconciled_history(self):
        memory = {}
        self.run_main(self.fake_rpc(), memory)
        output = memory['top_unstakers.json']
        self.assertEqual(output['ranking_days'], 7)
        self.assertEqual(output['history_days'], 90)
        wallet = output['wallets'][0]
        self.assertEqual(wallet['status'], 'complete')
        self.assertEqual(wallet['series']['transferred'][1], 20)
        self.assertEqual(wallet['series']['held'][-1], 80)
        self.assertEqual(sum(wallet['series']['sold']), 0)

    def test_failed_ranking_does_not_replace_last_output(self):
        previous = {'schema_version': 2, 'wallets': [{'address': WALLET, 'status': 'complete'}]}
        memory = {'top_unstakers.json': copy.deepcopy(previous)}
        rpc = self.fake_rpc()
        rpc.logs.side_effect = RuntimeError('RPC unavailable')
        with self.assertRaises(RuntimeError):
            self.run_main(rpc, memory)
        self.assertEqual(memory['top_unstakers.json'], previous)

    def test_balance_mismatch_is_unavailable_not_zero_activity(self):
        memory = {}
        rpc = self.fake_rpc()
        original_call = rpc.call.side_effect
        rpc.call.side_effect = lambda method, params: (hex(79 * activity.UNIT)
            if method == 'eth_call' and params[-1] == hex(1000) else original_call(method, params))
        self.run_main(rpc, memory)
        wallet = memory['top_unstakers.json']['wallets'][0]
        self.assertEqual(wallet['status'], 'unavailable')
        self.assertNotIn('series', wallet)

    def test_interrupted_history_keeps_previous_verified_series(self):
        memory = {}
        self.run_main(self.fake_rpc(), memory)
        verified = copy.deepcopy(memory['top_unstakers.json']['wallets'][0]['series'])
        rpc = self.fake_rpc()
        rpc.first_block.side_effect = [900, activity.BudgetExpired('Archive query paused')]
        with self.assertRaises(activity.BudgetExpired):
            self.run_main(rpc, memory)
        self.assertEqual(memory['top_unstakers.json']['wallets'][0]['status'], 'stale')
        self.assertEqual(memory['top_unstakers.json']['wallets'][0]['series'], verified)


class IndexedHistoryTests(unittest.TestCase):
    TX = '0x' + 'a' * 64

    def entry(self, tx=None, block=10, direction='outgoing'):
        return {'hash': tx or self.TX, 'blockNum': hex(block), 'rawContract': {'address': activity.VVV},
                'from': WALLET if direction == 'outgoing' else OTHER,
                'to': OTHER if direction == 'outgoing' else WALLET}

    def test_pagination_receipts_and_cross_direction_deduplication(self):
        rpc = Mock()
        tx2 = '0x' + 'b' * 64
        moves = [transfer(WALLET, OTHER, 30, tx=self.TX, block=10),
                 transfer(OTHER, WALLET, 10, tx=tx2, block=11)]
        rpc.asset_transfers.side_effect = [
            {'transfers': [self.entry()], 'pageKey': 'page2'},
            {'transfers': [self.entry()], 'pageKey': None},
            {'transfers': [self.entry(tx2, 11, 'incoming')]}]
        rpc.receipt.side_effect = [[moves[0]], [moves[1]]]
        cache = {}
        activity.indexed_history(rpc, cache, WALLET, 1, 20, Mock())
        self.assertEqual(rpc.receipt.call_count, 2)
        self.assertEqual(cache['history']['next_block'], 21)
        self.assertEqual(cache['history']['logs'], moves)
        self.assertEqual(cache['history']['source'], 'alchemy+rpc-receipts')
        self.assertNotIn('indexed_history', cache)

    def test_interrupted_page_resumes_frozen_range_and_does_not_refetch_receipt(self):
        rpc = Mock()
        rpc.asset_transfers.side_effect = [
            {'transfers': [self.entry()], 'pageKey': 'page2'},
            activity.BudgetExpired('Paused')]
        rpc.receipt.return_value = [transfer(WALLET, OTHER, 30, tx=self.TX, block=10)]
        cache = {}
        with self.assertRaises(activity.BudgetExpired):
            activity.indexed_history(rpc, cache, WALLET, 1, 20, Mock())
        self.assertEqual(cache['indexed_history']['directions']['outgoing']['page'], 'page2')
        rpc.asset_transfers.reset_mock(side_effect=True)
        rpc.asset_transfers.side_effect = [{'transfers': []}, {'transfers': []}]
        rpc.receipt.reset_mock()
        activity.indexed_history(rpc, cache, WALLET, 2, 30, Mock())
        self.assertEqual(rpc.asset_transfers.call_args_list[0].args, (WALLET, 1, 20, 'outgoing', 'page2'))
        self.assertEqual(cache['history']['next_block'], 21)
        rpc.receipt.assert_not_called()

    def test_foreign_token_and_receipt_mismatch_cannot_publish_complete_history(self):
        rpc = Mock()
        bad = self.entry()
        bad['rawContract']['address'] = QUOTE
        rpc.asset_transfers.return_value = {'transfers': [bad]}
        with self.assertRaisesRegex(RuntimeError, 'unrelated transaction'):
            activity.indexed_history(rpc, {}, WALLET, 1, 20, Mock())
        rpc.asset_transfers.return_value = {'transfers': [self.entry()]}
        rpc.receipt.return_value = [transfer(OTHER, ROUTER, 30, tx=self.TX, block=10)]
        cache = {}
        with self.assertRaisesRegex(RuntimeError, 'did not match'):
            activity.indexed_history(rpc, cache, WALLET, 1, 20, Mock())
        self.assertNotIn('history', cache)

    def test_index_unavailable_uses_archive_fallback(self):
        rpc = Mock()
        rpc.has_transfer_index = True
        rpc.chunk = 10000
        rpc.asset_transfers.side_effect = activity.IndexUnavailable('Method not enabled')
        rpc.logs.return_value = []
        cache = {}
        activity.wallet_history(rpc, cache, WALLET, 1, 20, Mock())
        self.assertEqual(cache['history']['next_block'], 21)
        self.assertEqual(rpc.logs.call_count, 2)
        self.assertFalse(rpc.has_transfer_index)

    def test_alchemy_query_is_pinned_and_token_filtered(self):
        with patch.dict(activity.os.environ, {'ALCHEMY_API_KEY': 'test-fixture-key'}):
            rpc = activity.RPC(10)
        rpc.call = Mock(return_value={'transfers': []})
        rpc.asset_transfers(WALLET, 1, 20, 'incoming', 'page2')
        method, params = rpc.call.call_args.args
        self.assertEqual(method, 'alchemy_getAssetTransfers')
        self.assertEqual(params[0]['contractAddresses'], [activity.VVV])
        self.assertEqual(params[0]['toBlock'], hex(20))
        self.assertEqual(params[0]['toAddress'], WALLET)
        self.assertEqual(params[0]['pageKey'], 'page2')


if __name__ == '__main__':
    unittest.main()
