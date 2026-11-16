"""Compile and deploy the settlement ledger to an in-memory EVM."""

import os
from pathlib import Path

os.environ['SOLCX_BINARY_PATH'] = str(Path(__file__).resolve().parents[1] / '.solcx')

import solcx
from eth_tester import EthereumTester, PyEVMBackend
from web3 import EthereumTesterProvider, Web3

SOLC_VERSION = '0.8.20'
OPTIMIZER_RUNS = 200


def compile_contract():
    source = Path(__file__).with_name('SettlementLedger.sol').read_text(encoding='utf-8')
    compiled = solcx.compile_source(source, output_values=['abi', 'bin'], solc_version=SOLC_VERSION,
                                    optimize=True, optimize_runs=OPTIMIZER_RUNS)
    return compiled['<stdin>:SettlementLedger']


def deploy_ledger(compiled=None):
    compiled = compiled or compile_contract()
    web3 = Web3(EthereumTesterProvider(EthereumTester(backend=PyEVMBackend())))
    factory = web3.eth.contract(abi=compiled['abi'], bytecode=compiled['bin'])
    receipt = web3.eth.wait_for_transaction_receipt(factory.constructor().transact({'from': web3.eth.accounts[0]}))
    if receipt.status != 1 or not receipt.contractAddress:
        raise RuntimeError('Contract deployment failed')
    return web3, web3.eth.contract(address=receipt.contractAddress, abi=compiled['abi']), receipt


def transact(web3, function, sender):
    receipt = web3.eth.wait_for_transaction_receipt(function.transact({'from': sender}))
    if receipt.status != 1:
        raise RuntimeError('Transaction failed')
    return receipt


def fund_account(web3, sender: str, recipient: str, amount_wei: int = 10**18):
    """Transfer test ETH on local Py-EVM so a generated ECDSA account can pay gas."""
    receipt = web3.eth.wait_for_transaction_receipt(
        web3.eth.send_transaction({'from': sender, 'to': recipient, 'value': amount_wei})
    )
    if receipt.status != 1:
        raise RuntimeError('Funding transaction failed')
    return receipt


def transact_signed(web3, function, account, *, gas: int = 1_000_000):
    """Build, sign with a local ECDSA private key, and broadcast a contract transaction."""
    tx = function.build_transaction({
        'from': account.address,
        'nonce': web3.eth.get_transaction_count(account.address),
        'gas': gas,
        'gasPrice': web3.eth.gas_price,
    })
    signed = account.sign_transaction(tx)
    tx_hash = web3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = web3.eth.wait_for_transaction_receipt(tx_hash)
    if receipt.status != 1:
        raise RuntimeError('Signed transaction failed')
    return receipt
