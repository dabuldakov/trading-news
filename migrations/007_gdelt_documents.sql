CREATE TABLE IF NOT EXISTS gdelt_documents (
    url TEXT PRIMARY KEY,
    published_at TIMESTAMPTZ NOT NULL,
    domain TEXT NOT NULL,
    tone REAL,
    themes TEXT,
    resolved BOOLEAN NOT NULL DEFAULT false,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS gdelt_documents_pending_idx
    ON gdelt_documents (resolved, published_at DESC);
