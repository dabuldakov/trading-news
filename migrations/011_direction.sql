ALTER TABLE news_tickers ADD COLUMN IF NOT EXISTS direction REAL;
ALTER TABLE news_tickers ADD COLUMN IF NOT EXISTS direction_model TEXT;
CREATE INDEX IF NOT EXISTS news_tickers_direction_idx ON news_tickers (direction);
