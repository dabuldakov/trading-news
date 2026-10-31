from __future__ import annotations

import os

os.environ.setdefault(
    "DATABASE_URL", "postgresql://news:news@127.0.0.1:5433/news"
)
os.environ.setdefault("SERVICE_KEY", "test-key")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from trading_news.api.app import app  # noqa: E402
from trading_news.db import migrate, pool  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _migrated() -> None:
    migrate()


@pytest.fixture(autouse=True)
def _clean() -> None:
    with pool().connection() as conn:
        conn.execute(
            """
            TRUNCATE news_articles, news_sources, aliases, instruments,
                     companies, sync_runs, news_analysis, signals,
                     event_markers, signal_watermark, sector_returns,
                     sector_paper_trades, gdelt_documents
            RESTART IDENTITY CASCADE
            """
        )
        conn.commit()
    yield


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as test_client:
        yield test_client
