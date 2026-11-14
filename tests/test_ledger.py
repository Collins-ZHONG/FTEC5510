from concurrent.futures import ThreadPoolExecutor
import sqlite3

from eth_account import Account
import pytest

from core.crypto import sign_record
from core.ledger_db import BudgetExceeded, DuplicateIntent, IdempotencyConflict, LedgerDB, PolicyRejected
from core.models import AuthorizationMandate, PaymentIntent


@pytest.fixture
def ledger(tmp_path):
    owner, agent, provider, other = (Account.create() for _ in range(4))
    mandate = AuthorizationMandate(
        mandate_id='mandate', owner=owner.address, agent=agent.address,
        session_cap=10, max_per_tx=5, allowed_endpoints=('data',), expires_at=200,
    )
    mandate = mandate.model_copy(update={'signature': sign_record(mandate, owner.key)})
    db = LedgerDB(tmp_path / 'ledger.db')
    db.initialize()
    db.register_mandate(mandate, trusted_owner=owner.address, endpoint_payees={'data': provider.address})
    return db, owner, agent, provider, other, mandate


def make_intent(ledger, number, amount=5, key=None):
    _, _, agent, provider, _, mandate = ledger
    intent = PaymentIntent(intent_id=f'intent-{number}', mandate_id=mandate.mandate_id,
                           endpoint='data', payee=provider.address, amount=amount,
                           nonce=f'nonce-{number}', idempotency_key=key or f'key-{number}')
    return intent.model_copy(update={'signature': sign_record(intent, agent.key)})


def test_concurrent_reservations(ledger):
    db = ledger[0]
    intents = [make_intent(ledger, i) for i in range(20)]

    def reserve(intent):
        try:
            db.admit_and_reserve(intent, now=100)
            return 'accepted'
        except BudgetExceeded:
            return 'rejected'

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(reserve, intents))
    assert results.count('accepted') == 2
    assert results.count('rejected') == 18
    with sqlite3.connect(db.path) as conn:
        assert conn.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        assert conn.execute('SELECT COUNT(*), SUM(amount) FROM payments').fetchone() == (2, 10)
        assert conn.execute('SELECT COUNT(*) FROM settlement_events').fetchone()[0] == 2
    print('Concurrent reservations: 2 accepted, 18 rejected; reserved total: 10')


def test_idempotency_and_revocation(ledger):
    db, _, agent, _, other, mandate = ledger
    intent = make_intent(ledger, 1)
    first, created = db.admit_and_reserve(intent, now=100)
    assert created
    db.revoke_mandate(mandate.mandate_id)
    repeated, created = db.admit_and_reserve(intent, now=300)
    assert not created and repeated == first
    changed = intent.model_copy(update={'amount': 4})
    changed = changed.model_copy(update={'signature': sign_record(changed, agent.key)})
    with pytest.raises(IdempotencyConflict):
        db.admit_and_reserve(changed, now=100)
    forged = intent.model_copy(update={'signature': sign_record(intent, other.key)})
    with pytest.raises(PolicyRejected, match='INVALID_INTENT_SIGNATURE'):
        db.admit_and_reserve(forged, now=100)
    with pytest.raises(PolicyRejected, match='REVOKED'):
        db.admit_and_reserve(make_intent(ledger, 2), now=100)


def test_expiry_duplicate_and_invalid_signature(ledger):
    db = ledger[0]
    intent = make_intent(ledger, 1)
    with pytest.raises(PolicyRejected, match='EXPIRED'):
        db.admit_and_reserve(intent, now=200)
    db.admit_and_reserve(intent, now=100)
    duplicate = make_intent(ledger, 1, key='different-key')
    with pytest.raises(DuplicateIntent):
        db.admit_and_reserve(duplicate, now=100)
    tampered = make_intent(ledger, 2).model_copy(update={'amount': 4})
    with pytest.raises(PolicyRejected, match='INVALID_INTENT_SIGNATURE'):
        db.admit_and_reserve(tampered, now=100)
    with sqlite3.connect(db.path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM payments').fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM settlement_events').fetchone()[0] == 1


@pytest.mark.parametrize('state,consumes_budget', [
    ('Settled', True), ('DeliveryUncertain', True), ('Cancelled', False),
])
def test_budget_counts_payment_states(ledger, state, consumes_budget):
    db = ledger[0]
    db.admit_and_reserve(make_intent(ledger, 1), now=100)
    db.admit_and_reserve(make_intent(ledger, 2), now=100)
    with sqlite3.connect(db.path) as conn:
        conn.execute('UPDATE payments SET state = ? WHERE intent_id = ?', (state, 'intent-1'))
    if consumes_budget:
        with pytest.raises(BudgetExceeded):
            db.admit_and_reserve(make_intent(ledger, 3), now=100)
    else:
        assert db.admit_and_reserve(make_intent(ledger, 3), now=100)[1]


def test_registration_checks_trusted_owner_and_signature(ledger, tmp_path):
    _, owner, _, provider, other, mandate = ledger
    db = LedgerDB(tmp_path / 'other.db')
    db.initialize()
    with pytest.raises(PolicyRejected):
        db.register_mandate(mandate, trusted_owner=other.address, endpoint_payees={'data': provider.address})
    forged = mandate.model_copy(update={'session_cap': 100})
    with pytest.raises(PolicyRejected):
        db.register_mandate(forged, trusted_owner=owner.address, endpoint_payees={'data': provider.address})
