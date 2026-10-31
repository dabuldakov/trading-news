from __future__ import annotations

import json

from trading_news.db import pool
from trading_news.nlp import direction_llm, relevance_llm


def _seed(conn):
    source = conn.execute(
        "INSERT INTO news_sources (name, kind) VALUES ('rbc', 'rss') RETURNING id"
    ).fetchone()[0]
    company = conn.execute(
        "INSERT INTO companies (name) VALUES ('Сбербанк') RETURNING id"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO instruments (ticker, secid, company_id) VALUES ('SBER','SBER',%s)",
        (company,),
    )
    article = conn.execute(
        """
        INSERT INTO news_articles (source_id,title,published_at,hash)
        VALUES (%s,'Сбербанк',now(),md5('gate')) RETURNING id
        """,
        (source,),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO news_analysis (article_id,event_type) VALUES (%s,'dividend')",
        (article,),
    )
    conn.execute(
        "INSERT INTO news_tickers (article_id,ticker,confidence,method,relevance) "
        "VALUES (%s,'SBER',0.95,'exact',NULL)",
        (article,),
    )
    conn.commit()
    return article


def test_direction_parse_and_apply() -> None:
    assert direction_llm._parse('{"scores":{"SBER":1.2,"X":-1}}', {"SBER"}) == {
        "SBER": 1.0
    }
    assert direction_llm._parse("bad", {"SBER"}) is None
    with pool().connection() as conn:
        article = _seed(conn)
    path = "/tmp/direction_test.jsonl"
    with open(path, "w") as fh:
        fh.write(json.dumps({"id": article, "scores": {"SBER": -0.4}}) + "\n")
    assert direction_llm.apply_predictions(path) == 1
    with pool().connection() as conn:
        assert (
            conn.execute(
                "SELECT direction FROM news_tickers WHERE article_id=%s", (article,)
            ).fetchone()[0]
            == -0.4
        )


def test_relevance_parse_and_apply() -> None:
    assert relevance_llm._parse('{"scores":{"SBER":1.2,"X":0}}', {"SBER"}) == {
        "SBER": 1.0
    }
    assert relevance_llm._parse("[]", {"SBER"}) is None
    with pool().connection() as conn:
        article = _seed(conn)
    path = "/tmp/relevance_test.jsonl"
    with open(path, "w") as fh:
        fh.write(json.dumps({"id": article, "scores": {"SBER": 1.0}}) + "\n")
    assert relevance_llm.apply_predictions(path) >= 1
    with pool().connection() as conn:
        assert (
            conn.execute(
                "SELECT relevance FROM news_tickers WHERE article_id=%s", (article,)
            ).fetchone()[0]
            == 1.0
        )
