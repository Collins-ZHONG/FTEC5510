from eth_account import Account
from pydantic import ValidationError
import pytest

from core.crypto import sign_record, verify_record
from core.models import AuthorizationMandate, PaymentIntent
from core.policy_guard import DecisionCode, check_payment


@pytest.fixture
def scenario():
    owner, agent, provider, other = (Account.create() for _ in range(4))
    mandate = AuthorizationMandate(
        mandate_id="mandate-1", owner=owner.address, agent=agent.address,
        session_cap=50, max_per_tx=15, allowed_endpoints=("data", "compute"),
        expires_at=200,
    )
    mandate = mandate.model_copy(update={"signature": sign_record(mandate, owner.key)})
    intent = PaymentIntent(
        intent_id="intent-1", mandate_id=mandate.mandate_id, endpoint="data",
        payee=provider.address, amount=2, nonce="nonce-1", idempotency_key="key-1",
    )
    intent = intent.model_copy(update={"signature": sign_record(intent, agent.key)})
    return owner, agent, provider, other, mandate, intent


def check(scenario, mandate=None, intent=None, **overrides):
    owner, _, provider, _, original_mandate, original_intent = scenario
    options = dict(trusted_owner=owner.address, endpoint_payees={"data": provider.address},
                   revoked_mandate_ids=set(), now=100)
    options.update(overrides)
    return check_payment(mandate or original_mandate, intent or original_intent, **options)


def signed_change(record, key, **updates):
    updated = record.model_copy(update=updates)
    return updated.model_copy(update={"signature": sign_record(updated, key)})


def test_c01_valid_purchase(scenario):
    assert check(scenario).code == DecisionCode.ALLOWED
    assert check(scenario).allowed


def test_c02_non_whitelisted_endpoint(scenario):
    _, agent, _, _, _, intent = scenario
    intent = signed_change(intent, agent.key, endpoint="unknown")
    assert check(scenario, intent=intent).code == DecisionCode.ENDPOINT_NOT_ALLOWED


def test_c02_wrong_payee_for_endpoint(scenario):
    _, agent, _, other, _, intent = scenario
    intent = signed_change(intent, agent.key, payee=other.address)
    assert check(scenario, intent=intent).code == DecisionCode.PAYEE_NOT_ALLOWED


def test_c03_single_payment_limit(scenario):
    _, agent, _, _, _, intent = scenario
    intent = signed_change(intent, agent.key, amount=16)
    assert check(scenario, intent=intent).code == DecisionCode.PRICE_LIMIT


def test_c04_expired_at_boundary(scenario):
    assert check(scenario, now=200).code == DecisionCode.EXPIRED


def test_c04_revoked(scenario):
    assert check(scenario, revoked_mandate_ids={"mandate-1"}).code == DecisionCode.REVOKED


@pytest.mark.parametrize("field,value", [("amount", 3), ("payee", "0x" + "12" * 20)])
def test_c05_tampered_intent(scenario, field, value):
    intent = scenario[-1].model_copy(update={field: value})
    assert check(scenario, intent=intent).code == DecisionCode.INVALID_INTENT_SIGNATURE


def test_tampered_authorization(scenario):
    mandate = scenario[-2].model_copy(update={"session_cap": 500})
    assert check(scenario, mandate=mandate).code == DecisionCode.INVALID_AUTHORIZATION_SIGNATURE


def test_self_declared_owner_is_not_trusted(scenario):
    _, _, _, other, mandate, _ = scenario
    mandate = signed_change(mandate, other.key, owner=other.address)
    assert check(scenario, mandate=mandate).code == DecisionCode.INVALID_OWNER


def test_wrong_agent_signature(scenario):
    _, _, _, other, _, intent = scenario
    intent = signed_change(intent, other.key)
    assert check(scenario, intent=intent).code == DecisionCode.INVALID_INTENT_SIGNATURE


def test_mandate_mismatch(scenario):
    intent = scenario[-1].model_copy(update={"mandate_id": "different"})
    assert check(scenario, intent=intent).code == DecisionCode.MANDATE_MISMATCH


@pytest.mark.parametrize("signature", ["", "0x1234", "not-a-signature"])
def test_malformed_signature(scenario, signature):
    intent = scenario[-1].model_copy(update={"signature": signature})
    assert not verify_record(intent, scenario[1].address)
    assert check(scenario, intent=intent).code == DecisionCode.INVALID_INTENT_SIGNATURE


@pytest.mark.parametrize("amount", [0, -1, 0.05, True, "2"])
def test_invalid_amount_rejected(scenario, amount):
    payload = scenario[-1].model_dump()
    payload["amount"] = amount
    with pytest.raises(ValidationError):
        PaymentIntent.model_validate(payload)
