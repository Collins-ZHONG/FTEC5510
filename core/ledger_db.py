"""Persistent authorization and atomic payment reservation on SQLite."""

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time
from collections.abc import Mapping

from eth_utils import is_address, is_same_address

from core.crypto import canonical_payload, verify_record
from core.models import AuthorizationMandate, PaymentIntent
from core.policy_guard import check_payment


class LedgerError(Exception):
    """Base class for rejected ledger operations."""


class PolicyRejected(LedgerError):
    pass


class BudgetExceeded(LedgerError):
    pass


class IdempotencyConflict(LedgerError):
    pass


class DuplicateIntent(LedgerError):
    pass


class LedgerDB:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path == ':memory:':
            raise ValueError('Use a file-backed database for separate connections')

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys = ON')
        try:
            yield conn
        finally:
            conn.close()

    def initialize(self):
        with self._connection() as conn:
            mode = conn.execute('PRAGMA journal_mode = WAL').fetchone()[0]
            if mode.lower() != 'wal':
                raise RuntimeError('SQLite WAL mode could not be enabled')
            conn.executescript(Path(__file__).with_name('schema.sql').read_text(encoding='utf-8'))

    def register_mandate(self, mandate: AuthorizationMandate, *, trusted_owner: str,
                         endpoint_payees: Mapping[str, str]):
        # model_copy(update=...) can bypass Pydantic validation.
        mandate = AuthorizationMandate.model_validate(mandate.model_dump())
        if (not is_address(trusted_owner) or not is_same_address(mandate.owner, trusted_owner)
                or not verify_record(mandate, trusted_owner)):
            raise PolicyRejected('INVALID_AUTHORIZATION')
        payees = dict(endpoint_payees)
        if (set(payees) != set(mandate.allowed_endpoints)
                or any(not isinstance(address, str) or not is_address(address) for address in payees.values())):
            raise ValueError('Each allowed endpoint must have a trusted payee')
        with self._connection() as conn:
            conn.execute('INSERT INTO mandates (mandate_id, mandate_json, trusted_owner, '
                         'endpoint_payees_json, session_cap, expires_at) VALUES (?, ?, ?, ?, ?, ?)',
                         (mandate.mandate_id, mandate.model_dump_json(), trusted_owner,
                          json.dumps(payees, sort_keys=True), mandate.session_cap, mandate.expires_at))

    def revoke_mandate(self, mandate_id: str):
        with self._connection() as conn:
            cursor = conn.execute('UPDATE mandates SET revoked = 1 WHERE mandate_id = ?', (mandate_id,))
            if cursor.rowcount != 1:
                raise KeyError(mandate_id)

    def admit_and_reserve(self, intent: PaymentIntent, *, now: int | None = None) -> tuple[dict, bool]:
        """Return (payment, created); policy and budget checks share the write lock."""
        intent = PaymentIntent.model_validate(intent.model_dump())
        payload = canonical_payload(intent)
        with self._connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            try:
                row = conn.execute('SELECT * FROM mandates WHERE mandate_id = ?',
                                   (intent.mandate_id,)).fetchone()
                if row is None:
                    raise PolicyRejected('UNKNOWN_MANDATE')
                mandate = AuthorizationMandate.model_validate_json(row['mandate_json'])
                if not verify_record(intent, mandate.agent):
                    raise PolicyRejected('INVALID_INTENT_SIGNATURE')

                existing = conn.execute('SELECT * FROM payments WHERE idempotency_key = ?',
                                        (intent.idempotency_key,)).fetchone()
                if existing:
                    if existing['request_payload'] != payload:
                        raise IdempotencyConflict('Idempotency key already used for another request')
                    conn.commit()
                    return dict(existing), False
                if conn.execute('SELECT 1 FROM payments WHERE intent_id = ?', (intent.intent_id,)).fetchone():
                    raise DuplicateIntent(intent.intent_id)

                current_time = int(time.time()) if now is None else now
                decision = check_payment(mandate, intent, trusted_owner=row['trusted_owner'],
                                         endpoint_payees=json.loads(row['endpoint_payees_json']),
                                         revoked_mandate_ids={row['mandate_id']} if row['revoked'] else set(),
                                         now=current_time)
                if not decision.allowed:
                    raise PolicyRejected(decision.code.value)
                used = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM payments "
                                    "WHERE mandate_id = ? AND state != 'Cancelled'",
                                    (intent.mandate_id,)).fetchone()[0]
                if used + intent.amount > row['session_cap']:
                    raise BudgetExceeded('Session budget exceeded')
                conn.execute('INSERT INTO payments (intent_id, mandate_id, idempotency_key, request_payload, '
                             'endpoint, payee, amount, state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                             (intent.intent_id, intent.mandate_id, intent.idempotency_key, payload,
                              intent.endpoint, intent.payee, intent.amount, 'Reserved', current_time))
                conn.execute('INSERT INTO settlement_events (intent_id, from_state, to_state, reason, created_at) '
                             'VALUES (?, NULL, ?, ?, ?)',
                             (intent.intent_id, 'Reserved', 'Admitted', current_time))
                payment = conn.execute('SELECT * FROM payments WHERE intent_id = ?', (intent.intent_id,)).fetchone()
                conn.commit()
                return dict(payment), True
            except Exception:
                conn.rollback()
                raise
