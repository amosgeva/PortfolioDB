-- 006: persisted price alerts, and an audit trail for deleted lots.
--
-- price_alerts (audit U-1). The dashboard's alerts used to live in a browser
-- array: armed, then lost on the refresh the page itself told the operator to
-- do. They are now rows. Manage → Alerts writes them (symbol validated, level
-- a finite positive number); the read-only views read them through the
-- payload. An alert is never deleted from the page, only deactivated, so the
-- history of what was armed when stays readable.
--
-- lot_deletions (audit A-9). "Append-only by design" was not what the Manage
-- page did: it deleted lots by row and by ID with nothing left behind. A
-- deletion is now recorded here — the deleted row in full, when, and the
-- reason the operator gave — in the same transaction as the DELETE itself
-- (app/native_writes.py::delete_lot), so a lot can never disappear without
-- its record, nor be recorded as deleted while it still exists.
--
-- Idempotent like the others: IF NOT EXISTS throughout, so re-running is a
-- no-op. Applied after schema.sql by app/apply_schema.py and by CI.

BEGIN;

CREATE TABLE IF NOT EXISTS price_alerts (
  id          BIGSERIAL PRIMARY KEY,
  symbol      TEXT NOT NULL REFERENCES instruments(symbol),
  direction   TEXT NOT NULL CHECK (direction IN ('above', 'below')),
  level       NUMERIC(20,8) NOT NULL CHECK (level > 0 AND level <> 'NaN'),
  note        TEXT,
  active      BOOLEAN NOT NULL DEFAULT TRUE,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The collector-side check and the payload read only the armed ones.
CREATE INDEX IF NOT EXISTS price_alerts_active_symbol_idx
  ON price_alerts (symbol) WHERE active;

CREATE TABLE IF NOT EXISTS lot_deletions (
  id          BIGSERIAL PRIMARY KEY,
  -- No foreign key: the lot this points at is, by definition, gone.
  lot_id      BIGINT NOT NULL,
  -- The whole row as it stood (to_jsonb of the deleted lots row), so the
  -- record survives later column changes and can be re-entered by hand.
  lot         JSONB NOT NULL,
  reason      TEXT NOT NULL CHECK (btrim(reason) <> ''),
  deleted_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS lot_deletions_deleted_at_idx
  ON lot_deletions (deleted_at DESC);

COMMIT;
