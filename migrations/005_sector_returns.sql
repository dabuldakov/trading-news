CREATE TABLE IF NOT EXISTS sector_returns (
    day DATE NOT NULL,
    sector TEXT NOT NULL,
    ret_1d REAL,
    n_tickers INT NOT NULL DEFAULT 0,
    PRIMARY KEY (day, sector)
);
CREATE INDEX IF NOT EXISTS sector_returns_sector_idx
    ON sector_returns (sector, day);
