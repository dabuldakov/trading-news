CREATE TABLE IF NOT EXISTS sector_paper_trades (
    id BIGSERIAL PRIMARY KEY,
    signal_day DATE NOT NULL,
    sector TEXT NOT NULL,
    sentiment REAL NOT NULL,
    n_news INT NOT NULL,
    horizon INT NOT NULL DEFAULT 1,
    entry_day DATE,
    ret REAL,
    excess REAL,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (signal_day, sector, horizon)
);
CREATE INDEX IF NOT EXISTS sector_paper_status_idx
    ON sector_paper_trades (status, entry_day);
