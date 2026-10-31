"""Макро-события: ключевая ставка ЦБ РФ.

CBR отдаёт ставку HTML-таблицей на ``hd_base/KeyRate``; из неё делаем статьи
событий (event_type=rate), чтобы макро-фон попадал в маркеры.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import httpx
from bs4 import BeautifulSoup

from ..db import pool
from .ingest import article_hash

logger = logging.getLogger(__name__)

CBR_KEY_RATE_URL = "https://www.cbr.ru/hd_base/KeyRate/"


def parse_key_rate(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    for row in soup.select("table tr"):
        cells = [cell.get_text(" ", strip=True) for cell in row.select("td")]
        if len(cells) < 2:
            continue
        date_raw, value_raw = cells[0], cells[1]
        try:
            date = datetime.strptime(date_raw, "%d.%m.%Y").replace(tzinfo=UTC)
            value = float(value_raw.replace(",", "."))
        except ValueError:
            continue
        out.append({"date": date, "value": value})
    return out


def _source_id(conn) -> int:
    row = conn.execute(
        """
        INSERT INTO news_sources (name, kind, url, credibility)
        VALUES ('cbr_key_rate', 'api', %s, 0.95)
        ON CONFLICT (name) DO UPDATE SET url = EXCLUDED.url
        RETURNING id
        """,
        (CBR_KEY_RATE_URL,),
    ).fetchone()
    return int(row[0])


def ingest_key_rate() -> dict:
    with httpx.Client(timeout=20.0) as client:
        response = client.get(CBR_KEY_RATE_URL)
        response.raise_for_status()
    rows = parse_key_rate(response.text)
    inserted = 0
    with pool().connection() as conn:
        source_id = _source_id(conn)
        for row in rows:
            title = f"ЦБ: ключевая ставка {row['value']:.2f}% (с {row['date'].date()})"
            url = f"{CBR_KEY_RATE_URL}#{row['date'].date()}"
            result = conn.execute(
                """
                INSERT INTO news_articles
                    (source_id, url, title, summary, published_at, lang, hash)
                VALUES (%s, %s, %s, '', %s, 'ru', %s)
                ON CONFLICT (hash) DO NOTHING
                RETURNING id
                """,
                (source_id, url, title, row["date"], article_hash(title, url)),
            ).fetchone()
            if result:
                inserted += 1
        conn.commit()
    from .link import link_pending

    linked = link_pending(limit=2000)
    return {"rates": len(rows), "inserted": inserted, "linked": linked}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(ingest_key_rate())
