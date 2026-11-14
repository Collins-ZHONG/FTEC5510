"""Signed records used for admission of a payment request."""

from typing import Literal

from eth_utils import is_address
from pydantic import BaseModel, ConfigDict, Field, field_validator


class SignedRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    signature: str = ""


class AuthorizationMandate(SignedRecord):
    record_type: Literal["authorization"] = "authorization"
    mandate_id: str = Field(min_length=1)
    owner: str
    agent: str
    session_cap: int = Field(gt=0)
    max_per_tx: int = Field(gt=0)
    allowed_endpoints: tuple[str, ...] = Field(min_length=1)
    expires_at: int = Field(gt=0)

    @field_validator("owner", "agent")
    @classmethod
    def validate_address(cls, value: str) -> str:
        if not is_address(value):
            raise ValueError("invalid Ethereum address")
        return value

    @field_validator("allowed_endpoints")
    @classmethod
    def validate_endpoints(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not endpoint.strip() for endpoint in value) or len(set(value)) != len(value):
            raise ValueError("endpoints must be nonempty and unique")
        return value


class PaymentIntent(SignedRecord):
    record_type: Literal["payment_intent"] = "payment_intent"
    intent_id: str = Field(min_length=1)
    mandate_id: str = Field(min_length=1)
    endpoint: str = Field(min_length=1)
    payee: str
    amount: int = Field(gt=0)
    nonce: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)

    @field_validator("payee")
    @classmethod
    def validate_payee(cls, value: str) -> str:
        if not is_address(value):
            raise ValueError("invalid Ethereum address")
        return value
