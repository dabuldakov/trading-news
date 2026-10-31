from __future__ import annotations

from trading_news.db import pool
from trading_news.news.link import (
    active_tickers,
    link_external,
    link_pending,
    link_pending_fast,
)


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
        "INSERT INTO aliases (company_id, alias, kind) "
        "VALUES (%s, 'Сбербанк', 'short_name')",
        (company,),
    )
    conn.execute(
        "INSERT INTO aliases (company_id, alias, kind) "
        "VALUES (%s, 'ПАО Сбербанк', 'issuer')",
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
            "INSERT INTO instruments (ticker, secid, company_id) "
            "VALUES ('BANK', 'BANK', %s)",
            (company,),
        )
        conn.execute(
            "INSERT INTO aliases (company_id, alias, kind) "
            "VALUES (%s, 'банк', 'short_name')",
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


def _sector_instrument(conn, ticker: str, status: str = "active") -> None:
    company = conn.execute(
        "INSERT INTO companies (name) VALUES (%s) RETURNING id",
        (f"{ticker} Co",),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO instruments (ticker, secid, company_id, status) "
        "VALUES (%s, %s, %s, %s)",
        (ticker, ticker, company, status),
    )


def test_active_tickers_filters_inactive() -> None:
    with pool().connection() as conn:
        _sector_instrument(conn, "GAZP", "active")
        _sector_instrument(conn, "ROSN", "inactive")
        conn.commit()
        assert active_tickers(conn) == {"GAZP"}


def test_link_external_links_only_active_sector_tickers() -> None:
    with pool().connection() as conn:
        _sector_instrument(conn, "GAZP", "active")
        _sector_instrument(conn, "ROSN", "inactive")
        article_id = _article(conn, "Цены на нефть выросли до максимума")
        conn.commit()
        linked = link_external(conn, article_id, "Цены на нефть выросли")
        conn.commit()
        rows = conn.execute(
            "SELECT ticker, method FROM news_tickers WHERE article_id = %s",
            (article_id,),
        ).fetchall()

    assert linked == 1
    assert rows == [("GAZP", "sector:oil")]


def test_link_pending_marks_sector_only_article_as_linked() -> None:
    with pool().connection() as conn:
        _sector_instrument(conn, "GAZP", "active")
        article_id = _article(conn, "Цены на нефть выросли до максимума")
        conn.commit()

    link_pending()

    with pool().connection() as conn:
        status = conn.execute(
            "SELECT status FROM news_articles WHERE id = %s", (article_id,)
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT ticker, method FROM news_tickers WHERE article_id = %s",
            (article_id,),
        ).fetchall()
    assert status == "linked"
    assert rows == [("GAZP", "sector:oil")]


def test_link_pending_skips_inactive_sector_tickers() -> None:
    with pool().connection() as conn:
        _sector_instrument(conn, "GAZP", "inactive")
        article_id = _article(conn, "Цены на нефть выросли до максимума")
        conn.commit()

    link_pending()

    with pool().connection() as conn:
        status = conn.execute(
            "SELECT status FROM news_articles WHERE id = %s", (article_id,)
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT ticker FROM news_tickers WHERE article_id = %s",
            (article_id,),
        ).fetchall()
    assert status == "skipped"
    assert rows == []


def test_link_pending_fast_links_exact_aliases() -> None:
    with pool().connection() as conn:
        _seed_company(conn)
        first = _article(conn, "Сбербанк выплатит дивиденды")
        second = _article(conn, "Погода в Москве улучшится")
        conn.commit()

    result = link_pending_fast(limit=10)

    assert result["processed"] == 2
    with pool().connection() as conn:
        linked = {
            row[0]
            for row in conn.execute(
                "SELECT ticker FROM news_tickers WHERE article_id = %s",
                (first,),
            ).fetchall()
        }
        statuses = dict(
            conn.execute(
                "SELECT id, status FROM news_articles WHERE id IN (%s, %s)",
                (first, second),
            ).fetchall()
        )
    assert {"SBER", "SBERP"} <= linked
    assert statuses[first] == "linked"
    assert statuses[second] == "skipped"


def test_alias_matching_rejects_substrings() -> None:
    from trading_news.news.link import _alias_hit

    assert not _alias_hit("UPS Raises Sales Outlook Ahead of Holiday Season", "HEAD")
    assert not _alias_hit("Wells Fargo Head of Central Banking", "HEAD")
    assert _alias_hit("$HEAD: HeadHunter reports results", "HEAD")
    assert not _alias_hit("Global carmakers search for new China strategy", "CARM")
    assert not _alias_hit("China’s battery factories are positive", "POSI")
    assert not _alias_hit("В аэропортах отменили ограничения, самолетов нет", "Самолет")
    assert not _alias_hit(
        "Самолет Ереван - Минеральные Воды подал сигнал тревоги после вылета",
        "Самолет",
    )
    assert _alias_hit("ГК Самолет отчиталась о росте выручки", "Самолет")
    assert not _alias_hit("Полковник выступил за отставку Макрона", "Акрон")


def test_alias_matching_keeps_russian_cases() -> None:
    from trading_news.news.link import _alias_hit

    assert _alias_hit("Сбербанк выплатит дивиденды", "Сбербанк")
    assert _alias_hit("Дивиденды Сбербанка выросли", "Сбербанк")


def test_sector_keywords_use_word_boundaries() -> None:
    from trading_news.news.link import SECTOR_KEYWORDS, _has_keyword

    assert not _has_keyword("Полковник выступил за отставку Макрона", "ставк")
    assert not _has_keyword("В компании осталось АО Тандер", "сталь")
    assert _has_keyword("Цены на нефть выросли", "нефть")
    assert "china" not in SECTOR_KEYWORDS


def test_link_is_valid_rechecks_old_signals() -> None:
    from trading_news.news.link import link_is_valid

    assert not link_is_valid(
        "UPS Raises Sales Outlook Ahead of Holiday Season", "HEAD", "exact", ["HEAD"]
    )
    assert link_is_valid("Сбербанк выплатит дивиденды", "SBER", "exact", ["Сбербанк"])
    assert not link_is_valid(
        "Полковник выступил за отставку Макрона", "SBER", "sector:rates"
    )
    assert link_is_valid("Цены на нефть выросли", "GAZP", "sector:oil")
