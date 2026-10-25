ALTER TABLE signals ADD COLUMN IF NOT EXISTS validated BOOLEAN NOT NULL DEFAULT false;
CREATE INDEX IF NOT EXISTS signals_validated_idx
    ON signals (validated, created_at DESC);
