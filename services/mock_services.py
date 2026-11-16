"""In-process mock API providers returning verifiable delivery digests."""

from dataclasses import dataclass
import hashlib
import json

from eth_utils import is_same_address

from core.models import PaymentIntent


class MockServiceError(Exception):
    """Base error for mock service invocation failures."""


class ServiceDeliveryFailed(MockServiceError):
    """Raised when a simulated upstream service fails to deliver."""


@dataclass(frozen=True)
class ServiceSpec:
    endpoint: str
    provider_name: str
    price: int
    payee: str


@dataclass(frozen=True)
class DeliveryReceipt:
    intent_id: str
    mandate_id: str
    endpoint: str
    payee: str
    amount: int
    payload: dict
    digest_sha256: str


DEFAULT_PRICES: dict[str, int] = {
    "data": 2,
    "compute": 15,
}


def compute_sha256_digest(payload: dict) -> str:
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return "0x" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class MockServiceRegistry:
    """Deterministic Data ($0.02) and Compute ($0.15) service providers."""

    def __init__(self, specs: dict[str, ServiceSpec]):
        self.specs = dict(specs)

    @classmethod
    def default(cls, *, data_payee: str, compute_payee: str) -> "MockServiceRegistry":
        return cls({
            "data": ServiceSpec(
                endpoint="data",
                provider_name="MockDataProvider",
                price=DEFAULT_PRICES["data"],
                payee=data_payee,
            ),
            "compute": ServiceSpec(
                endpoint="compute",
                provider_name="MockComputeProvider",
                price=DEFAULT_PRICES["compute"],
                payee=compute_payee,
            ),
        })

    def endpoint_payees(self) -> dict[str, str]:
        return {endpoint: spec.payee for endpoint, spec in self.specs.items()}

    def execute(
        self,
        intent: PaymentIntent,
        *,
        request_params: dict | None = None,
        simulate_failure: bool = False,
    ) -> DeliveryReceipt:
        spec = self.specs.get(intent.endpoint)
        if spec is None:
            raise MockServiceError(f"Unknown endpoint: {intent.endpoint}")
        if intent.amount != spec.price:
            raise MockServiceError(
                f"Price mismatch for {intent.endpoint}: expected {spec.price}, got {intent.amount}"
            )
        if not is_same_address(intent.payee, spec.payee):
            raise MockServiceError(
                f"Payee mismatch for {intent.endpoint}: expected {spec.payee}, got {intent.payee}"
            )
        if simulate_failure:
            raise ServiceDeliveryFailed(f"Simulated delivery timeout on {intent.endpoint}")

        params = dict(request_params or {})
        if intent.endpoint == "data":
            payload = {
                "service": spec.provider_name,
                "endpoint": "data",
                "intent_id": intent.intent_id,
                "mandate_id": intent.mandate_id,
                "unit_price": spec.price,
                "params": params,
                "result": {
                    "rows": 25,
                    "source": "hk-fx-sample-feed",
                    "status": "delivered",
                },
            }
        else:
            payload = {
                "service": spec.provider_name,
                "endpoint": "compute",
                "intent_id": intent.intent_id,
                "mandate_id": intent.mandate_id,
                "unit_price": spec.price,
                "params": params,
                "result": {
                    "job_type": "batch-risk-inference",
                    "tokens_processed": 1200,
                    "status": "completed",
                },
            }

        digest = compute_sha256_digest(payload)
        return DeliveryReceipt(
            intent_id=intent.intent_id,
            mandate_id=intent.mandate_id,
            endpoint=intent.endpoint,
            payee=spec.payee,
            amount=intent.amount,
            payload=payload,
            digest_sha256=digest,
        )
