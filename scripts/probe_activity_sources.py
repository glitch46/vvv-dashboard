#!/usr/bin/env python3
"""Small authenticated source check. Never print keys or credential-bearing URLs."""
import json
import os
import urllib.error
import urllib.request

from unstaker_activity import VVV


def probe(name, url, headers, method, params):
    report = {'provider': name, 'method': method}
    try:
        request = urllib.request.Request(url, data=json.dumps({
            'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}).encode(),
            headers={'Content-Type': 'application/json', **headers})
        with urllib.request.urlopen(request, timeout=15) as response:
            output = json.load(response)
        if output.get('error'):
            report.update(ok=False, error=output['error'])
        else:
            result = output.get('result')
            report['ok'] = result is not None
            if method == 'alchemy_getAssetTransfers' and isinstance(result, dict):
                report.update(transfers=len(result.get('transfers', [])), more_pages=bool(result.get('pageKey')))
            elif method == 'eth_chainId':
                report['chain_id'] = int(result, 16)
            elif method == 'eth_getBlockByNumber' and result:
                report['archive_block'] = int(result['number'], 16)
    except urllib.error.HTTPError as exc:
        report.update(ok=False, http_status=exc.code)
        try:
            output = json.loads(exc.read())
            report['error'] = output.get('error', output.get('message', 'HTTP request rejected'))
        except (ValueError, TypeError):
            pass
    except Exception as exc:
        report.update(ok=False, error_type=type(exc).__name__)
    encoded = json.dumps(report)
    for name in ('VENICE_API_KEY', 'ALCHEMY_API_KEY'):
        if os.environ.get(name):
            encoded = encoded.replace(os.environ[name], '[redacted]')
    print(encoded, flush=True)


if __name__ == '__main__':
    venice = os.environ.get('VENICE_API_KEY')
    if venice:
        url = os.environ.get('VENICE_RPC_URL', 'https://api.venice.ai/api/v1/crypto/rpc/base-mainnet')
        headers = {'Authorization': 'Bearer ' + venice}
        probe('Venice', url, headers, 'eth_chainId', [])
        probe('Venice', url, headers, 'eth_getBlockByNumber', [hex(50000000), False])
    alchemy = os.environ.get('ALCHEMY_API_KEY')
    if alchemy:
        url = 'https://base-mainnet.g.alchemy.com/v2/' + alchemy
        probe('Alchemy', url, {}, 'eth_chainId', [])
        probe('Alchemy', url, {}, 'alchemy_getAssetTransfers', [{
            'fromBlock': '0x0', 'toBlock': 'latest', 'fromAddress': '0xa87597a0aad36554a1dec8a09cc4724d99e4d2f8',
            'contractAddresses': [VVV], 'category': ['erc20'], 'excludeZeroValue': True,
            'maxCount': '0x1', 'order': 'desc'}])
