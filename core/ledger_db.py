"""Persistent authorization and atomic payment reservation on SQLite."""

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time
from collections.abc import Mapping, Sequence

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


class InvalidStateTransition(LedgerError):
    pass


class UnknownRecord(LedgerError):
    pass


PAYMENT_TRANSITIONS: dict[str, set[str]] = {
    "Reserved": {"Payable", "DeliveryUncertain", "Cancelled"},
    "DeliveryUncertain": {"Payable", "Cancelled"},
    "Payable": {"Batched"},
    "Batched": {"Submitted", "Payable"},
    "Submitted": {"Settled", "Batched"},
    "Cancelled": set(),
    "Settled": set(),
}


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

    @staticmethod
    def _transition_payment(
        conn: sqlite3.Connection,
        intent_id: str,
        to_state: str,
        reason: str,
        current_time: int,
        *,
        batch_id: str | None = None,
        clear_batch: bool = False,
    ) -> dict:
        row = conn.execute('SELECT * FROM payments WHERE intent_id = ?', (intent_id,)).fetchone()
        if row is None:
            raise UnknownRecord(intent_id)
        from_state = row['state']
        if to_state not in PAYMENT_TRANSITIONS.get(from_state, set()):
            raise InvalidStateTransition(
                f'Cannot transition payment {intent_id} from {from_state} to {to_state}'
            )
        if clear_batch:
            conn.execute(
                'UPDATE payments SET state = ?, batch_id = NULL WHERE intent_id = ?',
                (to_state, intent_id),
            )
        elif batch_id is not None:
            conn.execute(
                'UPDATE payments SET state = ?, batch_id = ? WHERE intent_id = ?',
                (to_state, batch_id, intent_id),
            )
        else:
            conn.execute(
                'UPDATE payments SET state = ? WHERE intent_id = ?',
                (to_state, intent_id),
            )
        conn.execute(
            'INSERT INTO settlement_events (intent_id, from_state, to_state, reason, created_at) '
            'VALUES (?, ?, ?, ?, ?)',
            (intent_id, from_state, to_state, reason, current_time),
        )
        updated = conn.execute('SELECT * FROM payments WHERE intent_id = ?', (intent_id,)).fetchone()
        return dict(updated)

    def mark_payable(
        self, intent_id: str, delivery_digest: str, *, now: int | None = None
    ) -> dict:
        """Transition a Reserved/DeliveryUncertain payment to Payable after verified delivery."""
        if not isinstance(delivery_digest, str) or not delivery_digest.strip():
            raise ValueError('delivery_digest must be non-empty')
        current_time = int(time.time()) if now is None else now
        with self._connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            try:
                payment = self._transition_payment(
                    conn,
                    intent_id,
                    'Payable',
                    f'Delivered:{delivery_digest.strip()}',
                    current_time,
                )
                conn.commit()
                return payment
            except Exception:
                conn.rollback()
                raise

    def mark_delivery_uncertain(
        self, intent_id: str, reason: str = 'DeliveryUncertain', *, now: int | None = None
    ) -> dict:
        """Transition a Reserved payment to DeliveryUncertain while keeping budget locked."""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError('reason must be non-empty')
        current_time = int(time.time()) if now is None else now
        with self._connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            try:
                payment = self._transition_payment(
                    conn,
                    intent_id,
                    'DeliveryUncertain',
                    reason.strip(),
                    current_time,
                )
                conn.commit()
                return payment
            except Exception:
                conn.rollback()
                raise

    def cancel_payment(
        self, intent_id: str, reason: str = 'Cancelled', *, now: int | None = None
    ) -> dict:
        """Cancel a Reserved/DeliveryUncertain payment and release its reserved budget."""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError('reason must be non-empty')
        current_time = int(time.time()) if now is None else now
        with self._connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            try:
                payment = self._transition_payment(
                    conn,
                    intent_id,
                    'Cancelled',
                    reason.strip(),
                    current_time,
                    clear_batch=True,
                )
                conn.commit()
                return payment
            except Exception:
                conn.rollback()
                raise

    def create_batch(
        self,
        mandate_id: str,
        batch_id: str,
        intent_ids: Sequence[str],
        records_digest: str,
        *,
        now: int | None = None,
    ) -> dict:
        """Group Payable payments under one mandate into a settlement batch."""
        if not isinstance(batch_id, str) or not batch_id.strip():
            raise ValueError('batch_id must be non-empty')
        if not isinstance(records_digest, str) or not records_digest.strip():
            raise ValueError('records_digest must be non-empty')
        if not intent_ids or len(set(intent_ids)) != len(intent_ids):
            raise ValueError('intent_ids must be non-empty and unique')
        current_time = int(time.time()) if now is None else now
        with self._connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            try:
                if not conn.execute('SELECT 1 FROM mandates WHERE mandate_id = ?', (mandate_id,)).fetchone():
                    raise UnknownRecord(mandate_id)
                if conn.execute('SELECT 1 FROM batches WHERE batch_id = ?', (batch_id,)).fetchone():
                    raise InvalidStateTransition(f'Batch already exists: {batch_id}')
                rows = []
                for intent_id in intent_ids:
                    row = conn.execute(
                        'SELECT * FROM payments WHERE intent_id = ?', (intent_id,)
                    ).fetchone()
                    if row is None:
                        raise UnknownRecord(intent_id)
                    if row['mandate_id'] != mandate_id:
                        raise PolicyRejected('MANDATE_MISMATCH')
                    if row['state'] != 'Payable':
                        raise InvalidStateTransition(
                            f'Payment {intent_id} must be Payable, got {row["state"]}'
                        )
                    rows.append(row)
                total_amount = sum(int(row['amount']) for row in rows)
                conn.execute(
                    'INSERT INTO batches (batch_id, mandate_id, total_amount, status, records_digest, tx_hash) '
                    'VALUES (?, ?, ?, ?, ?, NULL)',
                    (batch_id, mandate_id, total_amount, 'Batched', records_digest.strip()),
                )
                for intent_id in intent_ids:
                    self._transition_payment(
                        conn,
                        intent_id,
                        'Batched',
                        f'Batched:{batch_id}',
                        current_time,
                        batch_id=batch_id,
                    )
                batch = conn.execute(
                    'SELECT * FROM batches WHERE batch_id = ?', (batch_id,)
                ).fetchone()
                conn.commit()
                return dict(batch)
            except Exception:
                conn.rollback()
                raise

    def mark_batch_submitted(
        self, batch_id: str, tx_hash: str, *, now: int | None = None
    ) -> dict:
        """Record batch submission transaction hash and transition included payments to Submitted."""
        if not isinstance(tx_hash, str) or not tx_hash.strip():
            raise ValueError('tx_hash must be non-empty')
        tx_hash_clean = tx_hash.strip()
        current_time = int(time.time()) if now is None else now
        with self._connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            try:
                batch = conn.execute(
                    'SELECT * FROM batches WHERE batch_id = ?', (batch_id,)
                ).fetchone()
                if batch is None:
                    raise UnknownRecord(batch_id)
                if batch['status'] != 'Batched':
                    raise InvalidStateTransition(
                        f'Batch {batch_id} must be Batched, got {batch["status"]}'
                    )
                rows = conn.execute(
                    'SELECT intent_id FROM payments WHERE batch_id = ? '
                    'ORDER BY created_at ASC, intent_id ASC',
                    (batch_id,),
                ).fetchall()
                if not rows:
                    raise InvalidStateTransition(f'Batch {batch_id} has no payments')
                for row in rows:
                    self._transition_payment(
                        conn,
                        row['intent_id'],
                        'Submitted',
                        f'Submitted:{tx_hash_clean}',
                        current_time,
                    )
                conn.execute(
                    "UPDATE batches SET status = 'Submitted', tx_hash = ? WHERE batch_id = ?",
                    (tx_hash_clean, batch_id),
                )
                updated = conn.execute(
                    'SELECT * FROM batches WHERE batch_id = ?', (batch_id,)
                ).fetchone()
                conn.commit()
                return dict(updated)
            except Exception:
                conn.rollback()
                raise

    def mark_batch_settled(
        self, batch_id: str, tx_hash: str | None = None, *, now: int | None = None
    ) -> dict:
        """Confirm on-chain batch settlement and transition payments to Settled."""
        current_time = int(time.time()) if now is None else now
        with self._connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            try:
                batch = conn.execute(
                    'SELECT * FROM batches WHERE batch_id = ?', (batch_id,)
                ).fetchone()
                if batch is None:
                    raise UnknownRecord(batch_id)
                if batch['status'] != 'Submitted':
                    raise InvalidStateTransition(
                        f'Batch {batch_id} must be Submitted, got {batch["status"]}'
                    )
                recorded_hash = batch['tx_hash'] or ''
                if not recorded_hash:
                    raise InvalidStateTransition(f'Batch {batch_id} missing tx_hash')
                if tx_hash is not None and tx_hash.strip() != recorded_hash:
                    raise InvalidStateTransition(f'Batch {batch_id} tx_hash mismatch')
                rows = conn.execute(
                    'SELECT intent_id FROM payments WHERE batch_id = ? '
                    'ORDER BY created_at ASC, intent_id ASC',
                    (batch_id,),
                ).fetchall()
                if not rows:
                    raise InvalidStateTransition(f'Batch {batch_id} has no payments')
                for row in rows:
                    self._transition_payment(
                        conn,
                        row['intent_id'],
                        'Settled',
                        f'Settled:{recorded_hash}',
                        current_time,
                    )
                conn.execute(
                    "UPDATE batches SET status = 'Settled' WHERE batch_id = ?",
                    (batch_id,),
                )
                updated = conn.execute(
                    'SELECT * FROM batches WHERE batch_id = ?', (batch_id,)
                ).fetchone()
                conn.commit()
                return dict(updated)
            except Exception:
                conn.rollback()
                raise

    def get_payment(self, intent_id: str) -> dict:
        with self._connection() as conn:
            row = conn.execute(
                'SELECT * FROM payments WHERE intent_id = ?', (intent_id,)
            ).fetchone()
            if row is None:
                raise UnknownRecord(intent_id)
            return dict(row)

    def get_batch(self, batch_id: str) -> dict:
        with self._connection() as conn:
            row = conn.execute(
                'SELECT * FROM batches WHERE batch_id = ?', (batch_id,)
            ).fetchone()
            if row is None:
                raise UnknownRecord(batch_id)
            return dict(row)

    def get_payment_events(self, intent_id: str) -> list[dict]:
        with self._connection() as conn:
            if not conn.execute('SELECT 1 FROM payments WHERE intent_id = ?', (intent_id,)).fetchone():
                raise UnknownRecord(intent_id)
            rows = conn.execute(
                'SELECT * FROM settlement_events WHERE intent_id = ? ORDER BY event_id ASC',
                (intent_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_mandate_summary(self, mandate_id: str) -> dict:
        with self._connection() as conn:
            mandate = conn.execute(
                'SELECT * FROM mandates WHERE mandate_id = ?', (mandate_id,)
            ).fetchone()
            if mandate is None:
                raise UnknownRecord(mandate_id)
            state_rows = conn.execute(
                'SELECT state, COUNT(*) AS cnt, COALESCE(SUM(amount), 0) AS amt '
                'FROM payments WHERE mandate_id = ? GROUP BY state',
                (mandate_id,),
            ).fetchall()
            state_counts = {row['state']: int(row['cnt']) for row in state_rows}
            state_amounts = {row['state']: int(row['amt']) for row in state_rows}
            used_budget = sum(
                amt for state, amt in state_amounts.items() if state != 'Cancelled'
            )
            settled_amount = state_amounts.get('Settled', 0)
            session_cap = int(mandate['session_cap'])
            payee_rows = conn.execute(
                "SELECT payee, COALESCE(SUM(amount), 0) AS total "
                "FROM payments WHERE mandate_id = ? AND state = 'Settled' GROUP BY payee",
                (mandate_id,),
            ).fetchall()
            payee_settled_amounts = {row['payee']: int(row['total']) for row in payee_rows}
            return {
                'mandate_id': mandate_id,
                'session_cap': session_cap,
                'used_budget': used_budget,
                'remaining_budget': session_cap - used_budget,
                'settled_amount': settled_amount,
                'state_counts': state_counts,
                'state_amounts': state_amounts,
                'payee_settled_amounts': payee_settled_amounts,
            }
