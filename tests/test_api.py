from __future__ import annotations

from fastapi.testclient import TestClient

from trading_news.db import pool

HEADERS = {"X-Service-Key": "test-key"}


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": True}


def test_internal_requires_service_key(client: TestClient) -> None:
    assert client.get("/internal/instruments").status_code == 401
    assert (
        client.get(
            "/internal/instruments", headers={"X-Service-Key": "wrong"}
        ).status_code
        == 401
    )


def test_instruments_returns_active_rows(client: TestClient) -> None:
    with pool().connection() as conn:
        company = conn.execute(
            "INSERT INTO companies (name, inn) "
            "VALUES ('ПАО Сбербанк', '7707083893') RETURNING id"
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO instruments (ticker, secid, isin, name, company_id, lot)
            VALUES ('SBER', 'SBER', 'RU0009029540', 'Сбербанк ПАО', %s, 10)
            """,
            (company,),
        )
        conn.execute(
            """
            INSERT INTO instruments (ticker, secid, name, lot, status)
            VALUES ('OLD', 'OLD', 'Delisted', 1, 'inactive')
            """
        )
        conn.commit()

    response = client.get("/internal/instruments", headers=HEADERS)
    assert response.status_code == 200
    items = response.json()["instruments"]
    assert [item["ticker"] for item in items] == ["SBER"]
    assert items[0]["company"] == "ПАО Сбербанк"
    assert items[0]["lot"] == 10


def test_companies_returns_counts(client: TestClient) -> None:
    with pool().connection() as conn:
        company = conn.execute(
            "INSERT INTO companies (name) VALUES ('Газпром') RETURNING id"
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO instruments (ticker, secid, company_id)
            VALUES ('GAZP', 'GAZP', %s)
            """,
            (company,),
        )
        conn.commit()

    response = client.get("/internal/companies", headers=HEADERS)
    assert response.status_code == 200
    companies = response.json()["companies"]
    assert companies[0]["name"] == "Газпром"
    assert companies[0]["instruments"] == 1


def test_signals_hide_unvalidated_by_default(client: TestClient) -> None:
    with pool().connection() as conn:
        company = conn.execute(
            "INSERT INTO companies (name) VALUES ('ПАО Сбербанк') RETURNING id"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO instruments (ticker, secid, company_id) "
            "VALUES ('SBER', 'SBER', %s)",
            (company,),
        )
        article = conn.execute(
            """
            INSERT INTO news_articles (title, published_at, hash)
            VALUES ('Сбербанк', now(), md5('signal-article'))
            RETURNING id
            """
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO news_analysis (article_id, event_type) VALUES (%s, 'dividend')",
            (article,),
        )
        conn.execute(
            "INSERT INTO aliases (company_id, alias, kind) "
            "VALUES (%s, 'Сбербанк', 'short_name')",
            (company,),
        )
        conn.execute(
            "INSERT INTO news_tickers (article_id, ticker, confidence, method) "
            "VALUES (%s, 'SBER', 0.95, 'exact')",
            (article,),
        )
        for side, validated in (("buy", True), ("sell", False)):
            conn.execute(
                """
                INSERT INTO signals
                    (article_id, ticker, side, expected_edge, confidence,
                     horizon, marker_key, validated)
                VALUES (%s, 'SBER', %s, 0.01, 0.8, '1d', 'm', %s)
                """,
                (article, side, validated),
            )
        conn.commit()

    response = client.get("/internal/signals", headers=HEADERS)
    assert response.status_code == 200
    assert [row["validated"] for row in response.json()["signals"]] == [True]

    response = client.get(
        "/internal/signals?include_unvalidated=true", headers=HEADERS
    )
    assert len(response.json()["signals"]) == 2
