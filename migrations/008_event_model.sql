ALTER TABLE news_analysis
    ADD COLUMN IF NOT EXISTS event_model TEXT;

CREATE INDEX IF NOT EXISTS news_analysis_event_model_idx
    ON news_analysis (event_model);
