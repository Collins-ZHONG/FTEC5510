"""Canonical JSON and EIP-191 signing for the prototype's records."""

import json
import re

from eth_account import Account
from eth_account.messages import encode_defunct
from eth_keys.exceptions import BadSignature
from eth_utils import is_same_address
from eth_utils.exceptions import ValidationError

from core.models import SignedRecord


def canonical_payload(record: SignedRecord) -> str:
    return json.dumps(
        record.model_dump(mode="json", exclude={"signature"}),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def sign_record(record: SignedRecord, private_key: str | bytes) -> str:
    message = encode_defunct(text=canonical_payload(record))
    return Account.sign_message(message, private_key).signature.hex()


def verify_record(record: SignedRecord, expected_address: str) -> bool:
    try:
        if not re.fullmatch(r"(?:0x)?[0-9a-fA-F]{130}", record.signature):
            return False
        message = encode_defunct(text=canonical_payload(record))
        recovered = Account.recover_message(message, signature=record.signature)
        return is_same_address(recovered, expected_address)
    except (ValueError, TypeError, OverflowError, BadSignature, ValidationError):
        return False
