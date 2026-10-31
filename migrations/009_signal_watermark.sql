CREATE TABLE IF NOT EXISTS signal_watermark (
    name TEXT PRIMARY KEY,
    last_article_id BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
