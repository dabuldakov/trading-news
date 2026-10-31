from __future__ import annotations

import json

from trading_news.crypto_llm import _parse, apply_predictions, export_pending
from trading_news.crypto_reference import seed
from trading_news.db import pool


def _article(conn, ticker: str) -> int:
    source = conn.execute(
        "INSERT INTO news_sources (name, kind) VALUES ('coindesk', 'rss') RETURNING id"
    ).fetchone()[0]
    article = conn.execute(
        """
        INSERT INTO news_articles
            (source_id, title, summary, published_at, hash)
        VALUES (%s, 'Bitcoin upgrade', 'BTC news', now(), md5('crypto-llm'))
        RETURNING id
        """,
        (source,),
    ).fetchone()[0]
    conn.execute(
        """
        INSERT INTO news_tickers (article_id, ticker, confidence, method)
        VALUES (%s, %s, 0.95, 'exact')
        """,
        (article, ticker),
    )
    return article


def test_seed_crypto_reference() -> None:
    assert seed() == 20
    with pool().connection() as conn:
        count = conn.execute(
            "SELECT count(*) FROM instruments WHERE ticker LIKE '%:USDT'"
        ).fetchone()[0]
    assert count == 20


def test_parse_crypto_payload() -> None:
    parsed = _parse(
        '```json\n{"event_type":"upgrade","assets":{"BTC:USDT":'
        '{"relevance":1.2,"direction":0.4}}}\n```',
        {"BTC:USDT"},
    )
    assert parsed == {
        "event_type": "upgrade",
        "assets": {"BTC:USDT": {"relevance": 1.0, "direction": 0.4}},
    }
    assert _parse("not json", {"BTC:USDT"}) is None


def test_export_and_apply() -> None:
    seed()
    with pool().connection() as conn:
        article = _article(conn, "BTC:USDT")
        conn.commit()
    assert export_pending("/tmp/crypto_pending_test.jsonl", limit=10) == 1
    lines = open("/tmp/crypto_pending_test.jsonl", encoding="utf-8").read().splitlines()
    assert len(lines) == 1
    prediction = json.loads(lines[0])
    prediction["event_type"] = "upgrade"
    prediction["assets"] = {"BTC:USDT": {"relevance": 1.0, "direction": -0.2}}
    path = "/tmp/crypto_predictions_test.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(prediction) + "\n")
    assert apply_predictions(path) == 1
    with pool().connection() as conn:
        row = conn.execute(
            """
            SELECT n.event_type, t.relevance, t.direction
            FROM news_analysis n JOIN news_tickers t ON t.article_id = n.article_id
            WHERE n.article_id = %s
            """,
            (article,),
        ).fetchone()
    assert row == ("upgrade", 1.0, -0.2)
