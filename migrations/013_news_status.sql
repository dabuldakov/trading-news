CREATE TABLE IF NOT EXISTS news_status (
    profile    TEXT NOT NULL,
    stage      TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'ok',
    detail     TEXT NOT NULL DEFAULT '',
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (profile, stage)
);

ALTER TABLE news_analysis ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;
UPDATE news_analysis SET updated_at = created_at WHERE updated_at IS NULL;
ALTER TABLE news_analysis ALTER COLUMN updated_at SET DEFAULT now();
ALTER TABLE news_analysis ALTER COLUMN updated_at SET NOT NULL;
