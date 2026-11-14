CREATE TABLE IF NOT EXISTS mandates (
    mandate_id TEXT PRIMARY KEY,
    mandate_json TEXT NOT NULL,
    trusted_owner TEXT NOT NULL,
    endpoint_payees_json TEXT NOT NULL,
    session_cap INTEGER NOT NULL CHECK (session_cap > 0),
    expires_at INTEGER NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0 CHECK (revoked IN (0, 1))
);

CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY,
    mandate_id TEXT NOT NULL REFERENCES mandates(mandate_id),
    total_amount INTEGER NOT NULL CHECK (total_amount > 0),
    status TEXT NOT NULL CHECK (status IN ('Batched', 'Submitted', 'Settled', 'Failed')),
    records_digest TEXT NOT NULL,
    tx_hash TEXT
);

CREATE TABLE IF NOT EXISTS payments (
    intent_id TEXT PRIMARY KEY,
    mandate_id TEXT NOT NULL REFERENCES mandates(mandate_id),
    idempotency_key TEXT NOT NULL UNIQUE,
    request_payload TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    payee TEXT NOT NULL,
    amount INTEGER NOT NULL CHECK (amount > 0),
    state TEXT NOT NULL CHECK (state IN ('Reserved', 'DeliveryUncertain', 'Cancelled', 'Payable', 'Batched', 'Submitted', 'Settled')),
    batch_id TEXT REFERENCES batches(batch_id),
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS payments_mandate_state ON payments(mandate_id, state);

CREATE TABLE IF NOT EXISTS settlement_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id TEXT NOT NULL REFERENCES payments(intent_id),
    from_state TEXT,
    to_state TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
