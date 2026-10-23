from __future__ import annotations

from trading_news.db import pool
from trading_news.news.link import link_pending


def _seed_company(conn, name: str = "ПАО Сбербанк") -> int:
    company = conn.execute(
        "INSERT INTO companies (name) VALUES (%s) RETURNING id", (name,)
    ).fetchone()[0]
    for ticker in ("SBER", "SBERP"):
        conn.execute(
            """
            INSERT INTO instruments (ticker, secid, company_id)
            VALUES (%s, %s, %s)
            """,
            (ticker, ticker, company),
        )
    conn.execute(
        "INSERT INTO aliases (company_id, alias, kind) VALUES (%s, 'Сбербанк', 'short_name')",
        (company,),
    )
    conn.execute(
        "INSERT INTO aliases (company_id, alias, kind) VALUES (%s, 'ПАО Сбербанк', 'issuer')",
        (company,),
    )
    return company


def _article(conn, title: str) -> int:
    return conn.execute(
        """
        INSERT INTO news_articles (title, published_at, hash)
        VALUES (%s, now(), md5(%s))
        RETURNING id
        """,
        (title, title),
    ).fetchone()[0]


def test_link_matches_company_alias() -> None:
    with pool().connection() as conn:
        _seed_company(conn)
        article_id = _article(conn, "Сбербанк выплатит рекордные дивиденды")
        conn.commit()

    linked = link_pending()

    assert linked >= 1
    with pool().connection() as conn:
        tickers = {
            row[0]
            for row in conn.execute(
                "SELECT ticker FROM news_tickers WHERE article_id = %s",
                (article_id,),
            ).fetchall()
        }
        status = conn.execute(
            "SELECT status FROM news_articles WHERE id = %s", (article_id,)
        ).fetchone()[0]
    assert "SBER" in tickers
    assert status == "linked"


def test_link_skips_unrelated_article() -> None:
    with pool().connection() as conn:
        _seed_company(conn)
        article_id = _article(conn, "Погода в Москве улучшится к выходным")
        conn.commit()

    link_pending()

    with pool().connection() as conn:
        tickers = conn.execute(
            "SELECT ticker FROM news_tickers WHERE article_id = %s", (article_id,)
        ).fetchall()
        status = conn.execute(
            "SELECT status FROM news_articles WHERE id = %s", (article_id,)
        ).fetchone()[0]
    assert tickers == []
    assert status == "skipped"


def test_link_ignores_generic_alias() -> None:
    with pool().connection() as conn:
        company = conn.execute(
            "INSERT INTO companies (name) VALUES ('Банк') RETURNING id"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO instruments (ticker, secid, company_id) VALUES ('BANK', 'BANK', %s)",
            (company,),
        )
        conn.execute(
            "INSERT INTO aliases (company_id, alias, kind) VALUES (%s, 'банк', 'short_name')",
            (company,),
        )
        article_id = _article(conn, "Банк России сохранил ставку")
        conn.commit()

    link_pending()

    with pool().connection() as conn:
        tickers = conn.execute(
            "SELECT ticker FROM news_tickers WHERE article_id = %s", (article_id,)
        ).fetchall()
    assert tickers == []
