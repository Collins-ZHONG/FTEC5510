"""End-to-end interim verification bridging ECDSA controls, SQLite, mock services, and Py-EVM."""

from contextlib import nullcontext
from pathlib import Path
import json
import tempfile

from eth_account import Account
from web3 import Web3
from web3.logs import DISCARD

from contracts.evm_client import (
    OPTIMIZER_RUNS,
    SOLC_VERSION,
    compile_contract,
    deploy_ledger,
    fund_account,
    transact,
    transact_signed,
)
from core.crypto import sign_record
from core.ledger_db import LedgerDB
from core.models import AuthorizationMandate, PaymentIntent
from services.mock_services import MockServiceRegistry, compute_sha256_digest


PURCHASE_SEQUENCE: tuple[tuple[str, int], ...] = (
    ("data", 2),
    ("compute", 15),
    ("data", 2),
    ("data", 2),
    ("compute", 15),
)


def to_bytes32_id(identifier: str) -> bytes:
    return Web3.keccak(text=identifier)


def run_interim_check(
    *,
    db_path: Path | None = None,
    output_path: Path | None = Path("outputs/interim_summary.json"),
    compiled: dict | None = None,
) -> dict:
    """Execute the 5-purchase 50-cent E2E flow and assert full off-chain/on-chain reconciliation."""
    mandate_id = "mandate-interim-1"
    batch_id = "batch-interim-1"
    session_cap = 50
    max_per_tx = 15
    base_time = 100
    expires_at = 1000

    # Deterministic keys keep addresses reproducible and cryptographically unified across layers.
    owner_acct = Account.from_key(Web3.keccak(text="ftec5510-g18-owner"))
    agent_acct = Account.from_key(Web3.keccak(text="ftec5510-g18-agent"))
    gateway_acct = Account.from_key(Web3.keccak(text="ftec5510-g18-gateway"))
    data_provider_acct = Account.from_key(Web3.keccak(text="ftec5510-g18-data-provider"))
    compute_provider_acct = Account.from_key(Web3.keccak(text="ftec5510-g18-compute-provider"))

    registry = MockServiceRegistry.default(
        data_payee=data_provider_acct.address,
        compute_payee=compute_provider_acct.address,
    )
    endpoint_payees = registry.endpoint_payees()

    # 1. Deploy SettlementLedger.sol on an isolated in-memory Py-EVM chain and fund owner/gateway for gas.
    compiled = compiled or compile_contract()
    web3, contract, deploy_receipt = deploy_ledger(compiled)
    admin_address = web3.eth.accounts[0]
    fund_account(web3, admin_address, owner_acct.address)
    fund_account(web3, admin_address, gateway_acct.address)

    session_id_b32 = to_bytes32_id(mandate_id)
    credit_receipt = transact(
        web3,
        contract.functions.credit(owner_acct.address, session_cap),
        admin_address,
    )
    session_receipt = transact_signed(
        web3,
        contract.functions.createSession(session_id_b32, gateway_acct.address, session_cap),
        owner_acct,
    )

    owner_initial_onchain = int(contract.functions.balances(owner_acct.address).call())
    assert owner_initial_onchain == session_cap

    temp_ctx = tempfile.TemporaryDirectory() if db_path is None else nullcontext(db_path.parent)
    with temp_ctx as root_dir:
        resolved_db_path = Path(root_dir) / "interim_ledger.db" if db_path is None else Path(db_path)
        db = LedgerDB(resolved_db_path)
        db.initialize()

        # 2. Register Owner-signed AuthorizationMandate in trusted gateway LedgerDB.
        mandate = AuthorizationMandate(
            mandate_id=mandate_id,
            owner=owner_acct.address,
            agent=agent_acct.address,
            session_cap=session_cap,
            max_per_tx=max_per_tx,
            allowed_endpoints=("data", "compute"),
            expires_at=expires_at,
        )
        mandate = mandate.model_copy(update={"signature": sign_record(mandate, owner_acct.key)})
        db.register_mandate(
            mandate,
            trusted_owner=owner_acct.address,
            endpoint_payees=endpoint_payees,
        )

        # 3. Execute 5 sequential purchases: Reserved -> Mock delivery -> Payable.
        purchase_records = []
        batch_items_for_digest = []
        payees: list[str] = []
        amounts: list[int] = []

        for idx, (endpoint, amount) in enumerate(PURCHASE_SEQUENCE, start=1):
            intent_id = f"intent-interim-{idx:02d}"
            intent = PaymentIntent(
                intent_id=intent_id,
                mandate_id=mandate_id,
                endpoint=endpoint,
                payee=endpoint_payees[endpoint],
                amount=amount,
                nonce=f"nonce-{idx:02d}",
                idempotency_key=f"idem-interim-{idx:02d}",
            )
            intent = intent.model_copy(update={"signature": sign_record(intent, agent_acct.key)})

            reserved_row, created = db.admit_and_reserve(intent, now=base_time + idx)
            assert created and reserved_row["state"] == "Reserved"

            receipt = registry.execute(intent, request_params={"seq": idx})
            payable_row = db.mark_payable(intent_id, receipt.digest_sha256, now=base_time + idx)
            assert payable_row["state"] == "Payable"

            payees.append(receipt.payee)
            amounts.append(receipt.amount)
            batch_items_for_digest.append({
                "intent_id": intent_id,
                "endpoint": endpoint,
                "payee": receipt.payee,
                "amount": receipt.amount,
                "delivery_digest": receipt.digest_sha256,
            })
            purchase_records.append({
                "intent_id": intent_id,
                "endpoint": endpoint,
                "payee": receipt.payee,
                "amount": receipt.amount,
                "delivery_digest": receipt.digest_sha256,
            })

        # 4. Aggregate Payable payments into a single batch and settle on Py-EVM: Batched -> Submitted -> Settled.
        intent_ids = [item["intent_id"] for item in purchase_records]
        records_digest = compute_sha256_digest({"mandate_id": mandate_id, "items": batch_items_for_digest})
        batch_row = db.create_batch(
            mandate_id,
            batch_id,
            intent_ids,
            records_digest,
            now=base_time + 20,
        )
        assert batch_row["status"] == "Batched" and batch_row["total_amount"] == sum(amounts)

        batch_id_b32 = to_bytes32_id(batch_id)
        settlement_receipt = transact_signed(
            web3,
            contract.functions.settleBatch(session_id_b32, batch_id_b32, payees, amounts),
            gateway_acct,
        )
        tx_hash_hex = Web3.to_hex(settlement_receipt.transactionHash)

        submitted_row = db.mark_batch_submitted(batch_id, tx_hash_hex, now=base_time + 21)
        assert submitted_row["status"] == "Submitted"
        settled_batch = db.mark_batch_settled(batch_id, tx_hash_hex, now=base_time + 22)
        assert settled_batch["status"] == "Settled"

        # 5. Collect DB & on-chain state and perform strict multi-layer reconciliation.
        db_summary = db.get_mandate_summary(mandate_id)
        for item in purchase_records:
            payment = db.get_payment(item["intent_id"])
            events = db.get_payment_events(item["intent_id"])
            trail = [ev["to_state"] for ev in events]
            assert payment["state"] == "Settled"
            assert trail == ["Reserved", "Payable", "Batched", "Submitted", "Settled"]
            item["final_state"] = payment["state"]
            item["lifecycle_trail"] = trail

        onchain_owner_remaining = int(contract.functions.balances(owner_acct.address).call())
        onchain_owner_deducted = owner_initial_onchain - onchain_owner_remaining
        onchain_data_credit = int(contract.functions.balances(data_provider_acct.address).call())
        onchain_compute_credit = int(contract.functions.balances(compute_provider_acct.address).call())
        onchain_providers_total = onchain_data_credit + onchain_compute_credit
        onchain_session = contract.functions.sessions(session_id_b32).call()
        onchain_session_budget = int(onchain_session[2])
        onchain_session_spent = int(onchain_session[3])

        batch_events = contract.events.BatchSettled().process_receipt(settlement_receipt, errors=DISCARD)
        payment_events = contract.events.PaymentSettled().process_receipt(settlement_receipt, errors=DISCARD)
        assert len(batch_events) == 1
        assert len(payment_events) == len(PURCHASE_SEQUENCE)

        batch_event_total = int(batch_events[0]["args"]["total"])
        batch_event_count = int(batch_events[0]["args"]["count"])
        payment_events_total = sum(int(ev["args"]["amount"]) for ev in payment_events)
        expected_total = sum(amount for _, amount in PURCHASE_SEQUENCE)  # 36
        expected_remaining = session_cap - expected_total  # 14

        budget_equation_diff = session_cap - (db_summary["remaining_budget"] + db_summary["settled_amount"])
        settlement_equation_diff = max(
            abs(db_summary["settled_amount"] - onchain_owner_deducted),
            abs(onchain_owner_deducted - onchain_providers_total),
            abs(onchain_providers_total - onchain_session_spent),
            abs(onchain_session_spent - batch_event_total),
            abs(batch_event_total - payment_events_total),
        )
        reconciliation_diff = abs(budget_equation_diff) + abs(settlement_equation_diff)

        assert db_summary["remaining_budget"] == expected_remaining
        assert db_summary["settled_amount"] == expected_total
        assert onchain_owner_remaining == expected_remaining
        assert onchain_session_budget == session_cap
        assert onchain_data_credit == 6
        assert onchain_compute_credit == 30
        assert batch_event_count == len(PURCHASE_SEQUENCE)
        assert reconciliation_diff == 0

    summary = {
        "scenario": "interim_e2e_reconciliation",
        "solidity_version": SOLC_VERSION,
        "optimizer": {"enabled": True, "runs": OPTIMIZER_RUNS},
        "backend": "EthereumTesterProvider/Py-EVM",
        "reconciliation_passed": True,
        "reconciliation_diff": reconciliation_diff,
        "equations": {
            "budget_conservation": {
                "formula": "initial_cap == remaining_budget + settled_db",
                "initial_cap": session_cap,
                "remaining_budget": db_summary["remaining_budget"],
                "settled_db": db_summary["settled_amount"],
                "diff": budget_equation_diff,
            },
            "cross_layer_settlement": {
                "formula": "settled_db == onchain_owner_deducted == onchain_providers_total == onchain_session_spent == events_total",
                "settled_db": db_summary["settled_amount"],
                "onchain_owner_deducted": onchain_owner_deducted,
                "onchain_providers_total": onchain_providers_total,
                "onchain_session_spent": onchain_session_spent,
                "batch_event_total": batch_event_total,
                "payment_events_total": payment_events_total,
                "diff": settlement_equation_diff,
            },
        },
        "identities": {
            "admin": admin_address,
            "owner": owner_acct.address,
            "agent": agent_acct.address,
            "gateway": gateway_acct.address,
            "providers": {
                "data": data_provider_acct.address,
                "compute": compute_provider_acct.address,
            },
        },
        "contract": {
            "address": contract.address,
            "deploy_tx_hash": Web3.to_hex(deploy_receipt.transactionHash),
            "credit_tx_hash": Web3.to_hex(credit_receipt.transactionHash),
            "session_creation_tx_hash": Web3.to_hex(session_receipt.transactionHash),
            "session_id_text": mandate_id,
            "session_id_bytes32": Web3.to_hex(session_id_b32),
        },
        "batch": {
            "batch_id_text": batch_id,
            "batch_id_bytes32": Web3.to_hex(batch_id_b32),
            "records_digest": records_digest,
            "tx_hash": tx_hash_hex,
            "block_number": int(settlement_receipt.blockNumber),
            "gas_used": int(settlement_receipt.gasUsed),
            "status": settled_batch["status"],
            "purchase_count": batch_event_count,
            "total_amount": expected_total,
        },
        "accounting": {
            "unit": "cents",
            "initial_cap": session_cap,
            "remaining_budget": db_summary["remaining_budget"],
            "settled_db": db_summary["settled_amount"],
            "onchain_owner_initial": owner_initial_onchain,
            "onchain_owner_remaining": onchain_owner_remaining,
            "onchain_owner_deducted": onchain_owner_deducted,
            "onchain_provider_credits": {
                "data": onchain_data_credit,
                "compute": onchain_compute_credit,
            },
            "onchain_providers_total": onchain_providers_total,
            "db_provider_credits": {
                "data": db_summary["payee_settled_amounts"][data_provider_acct.address],
                "compute": db_summary["payee_settled_amounts"][compute_provider_acct.address],
            },
            "state_counts_final": db_summary["state_counts"],
        },
        "purchases": purchase_records,
    }

    if output_path is not None:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    return summary


def main() -> None:
    summary = run_interim_check()
    acc = summary["accounting"]
    batch = summary["batch"]
    contract = summary["contract"]
    print("=== FTEC5510 Group 18 · Interim E2E Check & Reconciliation ===")
    print(f"Contract:          {contract['address']} (Solidity {summary['solidity_version']})")
    print(f"Session ID:        {contract['session_id_text']} -> {contract['session_id_bytes32']}")
    print(f"Batch ID:          {batch['batch_id_text']} -> {batch['batch_id_bytes32']}")
    print(f"Settlement TxHash: {batch['tx_hash']} (gasUsed={batch['gas_used']})")
    print(f"Final DB States:   {acc['state_counts_final']} (purchases={len(summary['purchases'])})")
    print(
        f"Budget Equation:   initial={acc['initial_cap']} == remaining={acc['remaining_budget']} "
        f"+ settled_db={acc['settled_db']} (diff={summary['equations']['budget_conservation']['diff']})"
    )
    print(
        f"Cross-Layer Check: settled_db={acc['settled_db']} == owner_deducted={acc['onchain_owner_deducted']} "
        f"== providers_total={acc['onchain_providers_total']} "
        f"(data={acc['onchain_provider_credits']['data']}, compute={acc['onchain_provider_credits']['compute']}; "
        f"diff={summary['equations']['cross_layer_settlement']['diff']})"
    )
    print(f"Reconciliation:    PASS (diff = {summary['reconciliation_diff']})")
    print("Output JSON:       outputs/interim_summary.json")


if __name__ == "__main__":
    main()
