CREATE TABLE IF NOT EXISTS market_candles (
    profile    TEXT NOT NULL,
    ticker     TEXT NOT NULL,
    timeframe  TEXT NOT NULL,
    ts         TIMESTAMPTZ NOT NULL,
    open       REAL,
    high       REAL,
    low        REAL,
    close      REAL NOT NULL,
    volume     REAL,
    value      REAL,
    source     TEXT NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (profile, ticker, timeframe, ts)
);
CREATE INDEX IF NOT EXISTS market_candles_lookup_idx
    ON market_candles (profile, ticker, timeframe, ts DESC);
