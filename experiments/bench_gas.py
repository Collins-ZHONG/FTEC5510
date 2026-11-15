"""Compare settlement transaction gas using the same ten purchases."""

import json
from pathlib import Path

from web3 import Web3
from contracts.evm_client import OPTIMIZER_RUNS, SOLC_VERSION, compile_contract, deploy_ledger, transact


def receipt_data(receipt):
    return {'transaction_hash': Web3.to_hex(receipt.transactionHash), 'block_number': receipt.blockNumber,
            'status': receipt.status, 'gas_used': receipt.gasUsed,
            'receipt': json.loads(Web3.to_json(receipt))}


def run_scenario(compiled, batch_size):
    web3, contract, deployment = deploy_ledger(compiled)
    admin, owner, gateway, provider1, provider2 = web3.eth.accounts[:5]
    purchases = [(provider1 if i % 2 == 0 else provider2, 2 if i % 2 == 0 else 5) for i in range(10)]
    total = sum(amount for _, amount in purchases)
    session_id = Web3.keccak(text='gas-experiment-session')
    credit = transact(web3, contract.functions.credit(owner, total), admin)
    creation = transact(web3, contract.functions.createSession(session_id, gateway, total), owner)
    receipts = []
    for start in range(0, len(purchases), batch_size):
        chunk = purchases[start:start + batch_size]
        batch_id = Web3.keccak(text=f'batch-{start}')
        receipt = transact(web3, contract.functions.settleBatch(
            session_id, batch_id, [p for p, _ in chunk], [a for _, a in chunk]), gateway)
        receipts.append(receipt_data(receipt))
    assert contract.functions.balances(owner).call() == 0
    assert contract.functions.balances(provider1).call() == 10
    assert contract.functions.balances(provider2).call() == 25
    assert contract.functions.sessions(session_id).call()[3] == total
    before = [contract.functions.balances(a).call() for a in (owner, provider1, provider2)]
    first = purchases[:batch_size]
    duplicate_hash = contract.functions.settleBatch(
        session_id, Web3.keccak(text='batch-0'), [p for p, _ in first], [a for _, a in first]
    ).transact({'from': gateway, 'gas': 1000000})
    duplicate = web3.eth.wait_for_transaction_receipt(duplicate_hash)
    after = [contract.functions.balances(a).call() for a in (owner, provider1, provider2)]
    assert duplicate.status == 0 and before == after
    return {'batch_size': batch_size, 'purchase_count': len(purchases), 'settlement_transactions': len(receipts),
            'contract_address': contract.address, 'deployment': receipt_data(deployment),
            'credit': receipt_data(credit), 'session_creation': receipt_data(creation),
            'purchases': [{'payee': p, 'amount': a} for p, a in purchases],
            'settlement_receipts': receipts, 'total_gas': sum(r['gas_used'] for r in receipts),
            'duplicate_batch_receipt': receipt_data(duplicate), 'duplicate_balances_unchanged': before == after,
            'final_balances': {'owner': 0, 'provider1': 10, 'provider2': 25}}


def main():
    compiled = compile_contract()
    scenarios = [run_scenario(compiled, size) for size in (1, 5, 10)]
    baseline = scenarios[0]['total_gas']
    for item in scenarios:
        item['gas_per_purchase'] = item['total_gas'] / item['purchase_count']
        item['change_vs_individual_percent'] = (item['total_gas'] / baseline - 1) * 100
    result = {'solidity_version': SOLC_VERSION, 'optimizer': {'enabled': True, 'runs': OPTIMIZER_RUNS},
              'backend': 'EthereumTesterProvider/Py-EVM',
              'method': 'Fresh chain per scenario; same ordered purchases; no provider aggregation; settlement gas only.',
              'scenarios': scenarios}
    output = Path('outputs/gas_benchmark.json')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2), encoding='utf-8')
    for item in scenarios:
        print(f"N={item['batch_size']}: transactions={item['settlement_transactions']}, "
              f"total={item['total_gas']}, per purchase={item['gas_per_purchase']:.1f}, "
              f"change={item['change_vs_individual_percent']:+.2f}%")
    print(f'Receipts: {output}')


if __name__ == '__main__':
    main()
