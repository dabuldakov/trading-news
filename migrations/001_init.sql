CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS unaccent;

CREATE TABLE IF NOT EXISTS companies (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    short_name TEXT,
    inn TEXT,
    ogrn TEXT,
    sector TEXT,
    site TEXT,
    wikidata_id TEXT,
    description TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS instruments (
    ticker TEXT PRIMARY KEY,
    secid TEXT NOT NULL,
    isin TEXT,
    board TEXT NOT NULL DEFAULT 'TQBR',
    type TEXT NOT NULL DEFAULT 'share',
    name TEXT,
    company_id BIGINT REFERENCES companies(id),
    lot INT NOT NULL DEFAULT 1,
    currency TEXT NOT NULL DEFAULT 'RUB',
    list_level INT,
    short_enabled BOOLEAN,
    broker_available BOOLEAN,
    adv_rub NUMERIC,
    status TEXT NOT NULL DEFAULT 'active',
    source TEXT,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS instruments_company_idx ON instruments (company_id);
CREATE INDEX IF NOT EXISTS instruments_status_idx ON instruments (status);

CREATE TABLE IF NOT EXISTS aliases (
    id BIGSERIAL PRIMARY KEY,
    company_id BIGINT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    alias TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'name',
    lang TEXT NOT NULL DEFAULT 'ru',
    weight REAL NOT NULL DEFAULT 1.0,
    UNIQUE (company_id, alias, kind)
);
CREATE INDEX IF NOT EXISTS aliases_trgm ON aliases USING gin (alias gin_trgm_ops);

CREATE TABLE IF NOT EXISTS company_assets (
    id BIGSERIAL PRIMARY KEY,
    company_id BIGINT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    asset TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'subsidiary',
    share REAL,
    source TEXT,
    UNIQUE (company_id, asset, kind)
);

CREATE TABLE IF NOT EXISTS news_sources (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    url TEXT,
    credibility REAL NOT NULL DEFAULT 0.5,
    lang TEXT NOT NULL DEFAULT 'ru',
    poll_sec INT NOT NULL DEFAULT 300,
    enabled BOOLEAN NOT NULL DEFAULT true
);

CREATE TABLE IF NOT EXISTS news_articles (
    id BIGSERIAL PRIMARY KEY,
    source_id BIGINT REFERENCES news_sources(id),
    url TEXT,
    title TEXT NOT NULL,
    summary TEXT,
    body TEXT,
    published_at TIMESTAMPTZ NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    lang TEXT,
    hash TEXT NOT NULL UNIQUE,
    embedding vector(768),
    raw JSONB,
    status TEXT NOT NULL DEFAULT 'new'
);
CREATE INDEX IF NOT EXISTS news_articles_published_idx
    ON news_articles (published_at DESC);
CREATE INDEX IF NOT EXISTS news_articles_status_idx
    ON news_articles (status, published_at DESC);
CREATE INDEX IF NOT EXISTS news_articles_embedding_idx
    ON news_articles USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS news_tickers (
    article_id BIGINT NOT NULL REFERENCES news_articles(id) ON DELETE CASCADE,
    ticker TEXT NOT NULL REFERENCES instruments(ticker),
    confidence REAL NOT NULL DEFAULT 1.0,
    method TEXT NOT NULL DEFAULT 'alias',
    PRIMARY KEY (article_id, ticker)
);
CREATE INDEX IF NOT EXISTS news_tickers_ticker_idx ON news_tickers (ticker);

CREATE TABLE IF NOT EXISTS news_analysis (
    article_id BIGINT PRIMARY KEY REFERENCES news_articles(id) ON DELETE CASCADE,
    sentiment REAL NOT NULL DEFAULT 0.0,
    score REAL NOT NULL DEFAULT 0.0,
    event_type TEXT NOT NULL DEFAULT 'other',
    materiality REAL NOT NULL DEFAULT 0.0,
    surprise REAL,
    horizon TEXT,
    model TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS news_reactions (
    article_id BIGINT NOT NULL REFERENCES news_articles(id) ON DELETE CASCADE,
    ticker TEXT NOT NULL REFERENCES instruments(ticker),
    t0 TIMESTAMPTZ NOT NULL,
    ret_1m REAL,
    ret_5m REAL,
    ret_30m REAL,
    ret_1h REAL,
    ret_1d REAL,
    ret_3d REAL,
    ret_5d REAL,
    abnormal_ret REAL,
    volume_z REAL,
    mfe REAL,
    mae REAL,
    gap REAL,
    costs REAL,
    PRIMARY KEY (article_id, ticker)
);
CREATE INDEX IF NOT EXISTS news_reactions_ticker_idx
    ON news_reactions (ticker, t0 DESC);

CREATE TABLE IF NOT EXISTS event_markers (
    marker_key TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    source TEXT,
    sentiment TEXT,
    sector TEXT,
    regime TEXT,
    n INT NOT NULL DEFAULT 0,
    mean REAL,
    median REAL,
    t REAL,
    hit REAL,
    q05 REAL,
    q95 REAL,
    version INT NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS signals (
    id BIGSERIAL PRIMARY KEY,
    article_id BIGINT REFERENCES news_articles(id),
    ticker TEXT NOT NULL REFERENCES instruments(ticker),
    side TEXT NOT NULL,
    expected_edge REAL NOT NULL DEFAULT 0.0,
    confidence REAL NOT NULL DEFAULT 0.0,
    horizon TEXT,
    marker_key TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    executed BOOLEAN NOT NULL DEFAULT false,
    UNIQUE (article_id, ticker, side)
);
CREATE INDEX IF NOT EXISTS signals_created_idx ON signals (created_at DESC);
CREATE INDEX IF NOT EXISTS signals_executed_idx ON signals (executed, created_at);

CREATE TABLE IF NOT EXISTS sync_runs (
    id BIGSERIAL PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'running',
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    counts JSONB,
    error TEXT
);
