"""Stateless signature and policy checks; budget reservation is a separate step."""

from collections.abc import Mapping, Set
from dataclasses import dataclass
from enum import Enum

from eth_utils import is_address, is_same_address

from core.crypto import verify_record
from core.models import AuthorizationMandate, PaymentIntent


class DecisionCode(str, Enum):
    ALLOWED = "ALLOWED"
    INVALID_OWNER = "INVALID_OWNER"
    INVALID_AUTHORIZATION_SIGNATURE = "INVALID_AUTHORIZATION_SIGNATURE"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"
    MANDATE_MISMATCH = "MANDATE_MISMATCH"
    INVALID_INTENT_SIGNATURE = "INVALID_INTENT_SIGNATURE"
    ENDPOINT_NOT_ALLOWED = "ENDPOINT_NOT_ALLOWED"
    PAYEE_NOT_ALLOWED = "PAYEE_NOT_ALLOWED"
    PRICE_LIMIT = "PRICE_LIMIT"


@dataclass(frozen=True)
class PolicyDecision:
    code: DecisionCode

    @property
    def allowed(self) -> bool:
        return self.code == DecisionCode.ALLOWED


def check_payment(
    mandate: AuthorizationMandate,
    intent: PaymentIntent,
    *,
    trusted_owner: str,
    endpoint_payees: Mapping[str, str],
    revoked_mandate_ids: Set[str],
    now: int,
) -> PolicyDecision:
    """Check one request against trusted gateway state, without reserving funds."""
    if not is_address(trusted_owner) or not is_same_address(mandate.owner, trusted_owner):
        return PolicyDecision(DecisionCode.INVALID_OWNER)
    if not verify_record(mandate, trusted_owner):
        return PolicyDecision(DecisionCode.INVALID_AUTHORIZATION_SIGNATURE)
    if now >= mandate.expires_at:
        return PolicyDecision(DecisionCode.EXPIRED)
    if mandate.mandate_id in revoked_mandate_ids:
        return PolicyDecision(DecisionCode.REVOKED)
    if intent.mandate_id != mandate.mandate_id:
        return PolicyDecision(DecisionCode.MANDATE_MISMATCH)
    if not verify_record(intent, mandate.agent):
        return PolicyDecision(DecisionCode.INVALID_INTENT_SIGNATURE)
    if intent.endpoint not in mandate.allowed_endpoints:
        return PolicyDecision(DecisionCode.ENDPOINT_NOT_ALLOWED)
    payee = endpoint_payees.get(intent.endpoint)
    if not payee or not is_address(payee) or not is_same_address(intent.payee, payee):
        return PolicyDecision(DecisionCode.PAYEE_NOT_ALLOWED)
    if intent.amount > mandate.max_per_tx:
        return PolicyDecision(DecisionCode.PRICE_LIMIT)
    return PolicyDecision(DecisionCode.ALLOWED)
