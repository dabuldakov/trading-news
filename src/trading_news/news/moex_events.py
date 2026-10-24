"""MOEX ISS corporate events: dividends → news articles.

Бесплатный ISS отдаёт историю дивидендов по каждой бумаге. Из неё делаем
события-статьи, чтобы новостной конвейер видел дивидендные факты (и линковал
их на тикер).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import httpx

from ..config import settings
from ..db import pool
from .ingest import article_hash

logger = logging.getLogger(__name__)


def fetch_dividends(ticker: str, client: httpx.Client | None = None) -> list[dict]:
    url = f"{settings.moex_iss_base.rstrip('/')}/securities/{ticker}/dividends.json"
    params = {"iss.meta": "off", "iss.only": "dividends"}
    own = client is None
    client = client or httpx.Client(timeout=20.0)
    try:
        response = client.get(url, params=params)
        response.raise_for_status()
        payload = response.json()
    finally:
        if own:
            client.close()
    block = payload.get("dividends") or {}
    columns = block.get("columns") or []
    index = {name: i for i, name in enumerate(columns)}
    out: list[dict] = []
    for row in block.get("data") or []:
        def value(name: str, row=row, index=index):
            position = index.get(name)
            if position is None or position >= len(row):
                return None
            return row[position]

        out.append(
            {
                "registrydate": value("registrydate"),
                "value": value("value"),
                "currencyid": value("currencyid") or "RUB",
                "isin": value("isin"),
            }
        )
    return out


def _source_id(conn) -> int:
    row = conn.execute(
        """
        INSERT INTO news_sources (name, kind, url, credibility)
        VALUES ('moex_iss', 'api', %s, 0.95)
        ON CONFLICT (name) DO UPDATE SET url = EXCLUDED.url
        RETURNING id
        """,
        (settings.moex_iss_base,),
    ).fetchone()
    return int(row[0])


def ingest_dividends(limit: int = 100) -> dict:
    with pool().connection() as conn:
        tickers = [
            row[0]
            for row in conn.execute(
                """
                SELECT ticker FROM instruments
                WHERE status = 'active' ORDER BY ticker LIMIT %s
                """,
                (limit,),
            ).fetchall()
        ]

    inserted = 0
    with httpx.Client(timeout=20.0) as client:
        for ticker in tickers:
            try:
                dividends = fetch_dividends(ticker, client)
            except Exception as exc:  # noqa: BLE001 - one ticker must not break
                logger.warning("moex dividends %s: %s", ticker, exc)
                continue
            for dividend in dividends:
                registry = str(dividend.get("registrydate") or "")
                amount = dividend.get("value")
                if not registry or amount is None:
                    continue
                title = (
                    f"{ticker}: дивиденды {amount} "
                    f"{dividend.get('currencyid') or 'RUB'} на акцию "
                    f"(реестр {registry})"
                )
                url = f"https://www.moex.com/ru/issue.aspx?code={ticker}"
                try:
                    published = datetime.fromisoformat(registry).replace(
                        tzinfo=UTC
                    )
                except ValueError:
                    published = datetime.now(UTC)
                with pool().connection() as conn:
                    source_id = _source_id(conn)
                    row = conn.execute(
                        """
                        INSERT INTO news_articles
                            (source_id, url, title, summary, published_at,
                             lang, hash)
                        VALUES (%s, %s, %s, '', %s, 'ru', %s)
                        ON CONFLICT (hash) DO NOTHING
                        RETURNING id
                        """,
                        (
                            source_id,
                            url,
                            title,
                            published,
                            article_hash(title, url),
                        ),
                    ).fetchone()
                    if row:
                        inserted += 1
                    conn.commit()
    from .link import link_pending

    linked = link_pending(limit=5000)
    return {"tickers": len(tickers), "inserted": inserted, "linked": linked}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(ingest_dividends())
