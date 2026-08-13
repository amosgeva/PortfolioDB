-- 001: runtime settings (public-repo plan §7, Phase 2).
--
-- One row per setting. app/settings.py reads with precedence
-- DB → env var → default and a short-TTL cache; secrets never live here
-- (they stay in .env — the dashboard has no auth).

BEGIN;

CREATE TABLE IF NOT EXISTS settings (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMIT;
