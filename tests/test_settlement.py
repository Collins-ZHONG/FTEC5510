import pytest
from web3 import Web3
from web3.logs import DISCARD

from contracts.evm_client import compile_contract, deploy_ledger, transact


@pytest.fixture(scope='module')
def compiled():
    return compile_contract()


@pytest.fixture
def chain(compiled):
    web3, contract, _ = deploy_ledger(compiled)
    admin, owner, gateway, payee, other = web3.eth.accounts[:5]
    session = Web3.keccak(text='session')
    transact(web3, contract.functions.credit(owner, 10), admin)
    transact(web3, contract.functions.createSession(session, gateway, 10), owner)
    return web3, contract, admin, owner, gateway, payee, other, session


def send(web3, call, sender):
    return web3.eth.wait_for_transaction_receipt(call.transact({'from': sender, 'gas': 1000000}))


def test_settlement_and_replay(chain):
    web3, contract, _, owner, gateway, payee, _, session = chain
    batch = Web3.keccak(text='batch')
    call = contract.functions.settleBatch(session, batch, [payee, payee], [2, 3])
    receipt = send(web3, call, gateway)
    assert receipt.status == 1
    assert contract.functions.balances(owner).call() == 5
    assert contract.functions.balances(payee).call() == 5
    assert contract.functions.sessions(session).call()[3] == 5
    assert len(contract.events.PaymentSettled().process_receipt(receipt, errors=DISCARD)) == 2
    assert send(web3, call, gateway).status == 0
    assert contract.functions.balances(owner).call() == 5
    assert contract.functions.balances(payee).call() == 5


@pytest.mark.parametrize('failure', ['gateway', 'budget', 'balance', 'length', 'zero'])
def test_rejected_transactions_are_atomic(chain, failure):
    web3, contract, _, owner, gateway, payee, other, session = chain
    batch = Web3.keccak(text=failure)
    amounts = [11] if failure == 'budget' else [5]
    payees = [] if failure == 'length' else [payee]
    if failure == 'zero':
        amounts = [0]
    if failure == 'balance':
        # Another session can have a larger budget than the owner's current balance.
        session = Web3.keccak(text='larger-budget')
        transact(web3, contract.functions.createSession(session, gateway, 20), owner)
        amounts = [11]
    call = contract.functions.settleBatch(session, batch, payees, amounts)
    receipt = send(web3, call, other if failure == 'gateway' else gateway)
    assert receipt.status == 0
    assert contract.functions.balances(owner).call() == 10
    assert contract.functions.balances(payee).call() == 0
    assert contract.functions.sessions(session).call()[3] == 0
    assert not contract.functions.processedBatches(session, batch).call()


def test_only_administrator_can_credit_and_session_owner_is_caller(chain):
    web3, contract, _, owner, gateway, _, other, session = chain
    assert send(web3, contract.functions.credit(other, 100), other).status == 0
    assert contract.functions.balances(other).call() == 0
    assert contract.functions.sessions(session).call()[:3] == [owner, gateway, 10]
    assert send(web3, contract.functions.createSession(session, other, 50), other).status == 0
    assert contract.functions.sessions(session).call()[0] == owner
