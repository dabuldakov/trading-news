from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from trading_news.config import settings
from trading_news.crypto_reactions import run
from trading_news.db import pool


def test_run_builds_crypto_reaction(monkeypatch) -> None:
    monkeypatch.setattr(settings, "news_profile", "crypto")
    now = datetime.now(UTC)
    with pool().connection() as conn:
        source = conn.execute(
            "INSERT INTO news_sources (name, kind) VALUES ('coindesk', 'rss') RETURNING id"
        ).fetchone()[0]
        company = conn.execute(
            "INSERT INTO companies (name) VALUES ('Bitcoin') RETURNING id"
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO instruments (ticker, secid, company_id, type, currency)
            VALUES ('BTC:USDT', 'BTC:USDT', %s, 'crypto', 'USD')
            """,
            (company,),
        )
        article = conn.execute(
            """
            INSERT INTO news_articles (source_id, title, published_at, hash)
            VALUES (%s, 'Bitcoin upgrade', %s, md5('reaction'))
            RETURNING id
            """,
            (source, now - timedelta(hours=1)),
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO news_tickers (article_id, ticker, confidence, method, relevance)
            VALUES (%s, 'BTC:USDT', 0.95, 'exact', 1.0)
            """,
            (article,),
        )
        conn.commit()

    def fake_candles(_client, _symbol, _bar, _limit):
        return [
            (int((now - timedelta(hours=2)).timestamp() * 1000), 100.0),
            (int((now - timedelta(hours=1)).timestamp() * 1000), 101.0),
            (int(now.timestamp() * 1000), 102.0),
        ]

    monkeypatch.setattr("trading_news.crypto_reactions._candles", fake_candles)
    result = run(limit=10)
    assert result == {"rows": 1, "built": 1}
    with pool().connection() as conn:
        row = conn.execute(
            "SELECT ret_1d, abnormal_ret FROM news_reactions WHERE article_id = %s",
            (article,),
        ).fetchone()
    assert row[0] == pytest.approx(102 / 101 - 1)
    assert row[1] is not None
