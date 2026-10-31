ALTER TABLE news_tickers
    ADD COLUMN IF NOT EXISTS relevance REAL;
ALTER TABLE news_tickers
    ADD COLUMN IF NOT EXISTS relevance_model TEXT;
CREATE INDEX IF NOT EXISTS news_tickers_relevance_idx
    ON news_tickers (relevance);
