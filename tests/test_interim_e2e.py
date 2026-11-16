"""End-to-end integration and state-machine regression tests for Step 4."""

import json
import sqlite3

from eth_account import Account
import pytest
from web3 import Web3

from contracts.evm_client import compile_contract, deploy_ledger, transact
from core.crypto import sign_record
from core.ledger_db import (
    BudgetExceeded,
    InvalidStateTransition,
    LedgerDB,
    PolicyRejected,
    UnknownRecord,
)
from core.models import AuthorizationMandate, PaymentIntent
from experiments.run_check import run_interim_check, to_bytes32_id
from services.mock_services import MockServiceError, MockServiceRegistry, ServiceDeliveryFailed


@pytest.fixture(scope="module")
def compiled_contract():
    return compile_contract()


def _make_signed_mandate(owner, agent, *, mandate_id="m-1", cap=20, max_tx=15):
    mandate = AuthorizationMandate(
        mandate_id=mandate_id,
        owner=owner.address,
        agent=agent.address,
        session_cap=cap,
        max_per_tx=max_tx,
        allowed_endpoints=("data", "compute"),
        expires_at=500,
    )
    return mandate.model_copy(update={"signature": sign_record(mandate, owner.key)})


def _make_signed_intent(agent, mandate_id, endpoint, payee, amount, num):
    intent = PaymentIntent(
        intent_id=f"{mandate_id}-intent-{num}",
        mandate_id=mandate_id,
        endpoint=endpoint,
        payee=payee,
        amount=amount,
        nonce=f"nonce-{num}",
        idempotency_key=f"idem-{mandate_id}-{num}",
    )
    return intent.model_copy(update={"signature": sign_record(intent, agent.key)})


def test_e2e_five_purchases_reconciliation_and_summary(tmp_path, compiled_contract):
    db_file = tmp_path / "interim.db"
    summary_file = tmp_path / "interim_summary.json"
    summary = run_interim_check(
        db_path=db_file,
        output_path=summary_file,
        compiled=compiled_contract,
    )

    assert summary_file.exists()
    loaded = json.loads(summary_file.read_text(encoding="utf-8"))
    assert loaded["reconciliation_passed"] is True
    assert loaded["reconciliation_diff"] == 0
    assert loaded["accounting"]["initial_cap"] == 50
    assert loaded["accounting"]["remaining_budget"] == 14
    assert loaded["accounting"]["settled_db"] == 36
    assert loaded["accounting"]["onchain_owner_deducted"] == 36
    assert loaded["accounting"]["onchain_provider_credits"] == {"data": 6, "compute": 30}
    assert loaded["accounting"]["state_counts_final"] == {"Settled": 5}
    assert loaded["batch"]["status"] == "Settled"
    assert len(loaded["purchases"]) == 5

    with sqlite3.connect(db_file) as conn:
        events_count = conn.execute("SELECT COUNT(*) FROM settlement_events").fetchone()[0]
        assert events_count == 25  # 5 transitions per payment x 5 payments


def test_state_machine_rejects_invalid_transitions(tmp_path):
    owner, agent, data_payee, compute_payee = (Account.create() for _ in range(4))
    db = LedgerDB(tmp_path / "states.db")
    db.initialize()
    mandate = _make_signed_mandate(owner, agent)
    payees = {"data": data_payee.address, "compute": compute_payee.address}
    db.register_mandate(mandate, trusted_owner=owner.address, endpoint_payees=payees)

    intent = _make_signed_intent(agent, mandate.mandate_id, "data", data_payee.address, 2, 1)
    db.admit_and_reserve(intent, now=100)

    # Cannot batch a Reserved payment before delivery becomes Payable.
    with pytest.raises(InvalidStateTransition):
        db.create_batch(mandate.mandate_id, "b-bad", [intent.intent_id], "0x" + "aa" * 32, now=101)

    # Cannot submit or settle an unknown batch.
    with pytest.raises(UnknownRecord):
        db.mark_batch_submitted("b-missing", "0x" + "bb" * 32, now=101)

    db.mark_payable(intent.intent_id, "0x" + "11" * 32, now=102)
    # Cannot mark DeliveryUncertain after already Payable, nor cancel directly from Payable.
    with pytest.raises(InvalidStateTransition):
        db.mark_delivery_uncertain(intent.intent_id, now=103)
    with pytest.raises(InvalidStateTransition):
        db.cancel_payment(intent.intent_id, now=103)

    db.create_batch(mandate.mandate_id, "b-1", [intent.intent_id], "0x" + "22" * 32, now=104)
    # Cannot settle a Batched batch before marking it Submitted with tx_hash.
    with pytest.raises(InvalidStateTransition):
        db.mark_batch_settled("b-1", now=105)


def test_delivery_uncertain_locks_budget_until_cancelled(tmp_path):
    owner, agent, data_payee, compute_payee = (Account.create() for _ in range(4))
    db = LedgerDB(tmp_path / "uncertain.db")
    db.initialize()
    mandate = _make_signed_mandate(owner, agent, cap=20, max_tx=15)
    registry = MockServiceRegistry.default(
        data_payee=data_payee.address,
        compute_payee=compute_payee.address,
    )
    db.register_mandate(
        mandate,
        trusted_owner=owner.address,
        endpoint_payees=registry.endpoint_payees(),
    )

    intent1 = _make_signed_intent(agent, mandate.mandate_id, "compute", compute_payee.address, 15, 1)
    db.admit_and_reserve(intent1, now=100)

    with pytest.raises(ServiceDeliveryFailed):
        registry.execute(intent1, simulate_failure=True)
    uncertain = db.mark_delivery_uncertain(intent1.intent_id, reason="Timeout", now=101)
    assert uncertain["state"] == "DeliveryUncertain"

    # Remaining cap is only 5, so a second 15-cent compute request must still be blocked.
    intent2 = _make_signed_intent(agent, mandate.mandate_id, "compute", compute_payee.address, 15, 2)
    with pytest.raises(BudgetExceeded):
        db.admit_and_reserve(intent2, now=102)

    # Cancelling the uncertain payment frees the 15 cents so intent2 now fits cleanly.
    cancelled = db.cancel_payment(intent1.intent_id, reason="RecoveredCancel", now=103)
    assert cancelled["state"] == "Cancelled"
    row2, created2 = db.admit_and_reserve(intent2, now=104)
    assert created2 and row2["state"] == "Reserved"


def test_batch_integrity_and_replay_guard(tmp_path, compiled_contract):
    owner, agent, data_payee, compute_payee = (Account.create() for _ in range(4))
    db = LedgerDB(tmp_path / "batch_guard.db")
    db.initialize()
    payees = {"data": data_payee.address, "compute": compute_payee.address}

    m1 = _make_signed_mandate(owner, agent, mandate_id="m-1")
    m2 = _make_signed_mandate(owner, agent, mandate_id="m-2")
    db.register_mandate(m1, trusted_owner=owner.address, endpoint_payees=payees)
    db.register_mandate(m2, trusted_owner=owner.address, endpoint_payees=payees)

    i1 = _make_signed_intent(agent, "m-1", "data", data_payee.address, 2, 1)
    i2 = _make_signed_intent(agent, "m-2", "data", data_payee.address, 2, 2)
    db.admit_and_reserve(i1, now=100)
    db.admit_and_reserve(i2, now=100)
    db.mark_payable(i1.intent_id, "0x" + "11" * 32, now=101)
    db.mark_payable(i2.intent_id, "0x" + "22" * 32, now=101)

    with pytest.raises(PolicyRejected, match="MANDATE_MISMATCH"):
        db.create_batch("m-1", "cross-batch", [i1.intent_id, i2.intent_id], "0x" + "33" * 32, now=102)

    batch = db.create_batch("m-1", "batch-1", [i1.intent_id], "0x" + "44" * 32, now=103)
    assert batch["total_amount"] == 2

    # Replay on DB and contract both fail safely.
    web3, contract, _ = deploy_ledger(compiled_contract)
    admin, chain_owner, chain_gateway = web3.eth.accounts[:3]
    session_id = to_bytes32_id("m-1")
    batch_id = to_bytes32_id("batch-1")
    transact(web3, contract.functions.credit(chain_owner, 10), admin)
    transact(web3, contract.functions.createSession(session_id, chain_gateway, 10), chain_owner)
    receipt = transact(
        web3,
        contract.functions.settleBatch(session_id, batch_id, [data_payee.address], [2]),
        chain_gateway,
    )
    tx_hex = Web3.to_hex(receipt.transactionHash)
    db.mark_batch_submitted("batch-1", tx_hex, now=104)
    db.mark_batch_settled("batch-1", tx_hex, now=105)

    with pytest.raises(InvalidStateTransition):
        db.mark_batch_submitted("batch-1", tx_hex, now=106)
    replay_tx = contract.functions.settleBatch(
        session_id, batch_id, [data_payee.address], [2]
    ).transact({"from": chain_gateway, "gas": 1_000_000})
    assert web3.eth.wait_for_transaction_receipt(replay_tx).status == 0
    assert contract.functions.balances(chain_owner).call() == 8


def test_mock_service_rejects_price_tampering():
    agent, data_payee, compute_payee = (Account.create() for _ in range(3))
    registry = MockServiceRegistry.default(
        data_payee=data_payee.address,
        compute_payee=compute_payee.address,
    )
    wrong_price = _make_signed_intent(agent, "m-1", "data", data_payee.address, 5, 9)
    with pytest.raises(MockServiceError, match="Price mismatch"):
        registry.execute(wrong_price)
