"""GDELT backfill: исторические новости по алиасам компаний.

Free API, russian sources. Rate limit ~1 request / 5 s, so the backfill is slow
by design; it is meant to be run as a one-off job (or nightly in small batches).
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import UTC, datetime, timedelta

import httpx

from ..db import pool
from .ingest import article_hash

logger = logging.getLogger(__name__)

BASE = "https://api.gdeltproject.org/api/v2/doc/doc"


def parse_articles(payload: dict) -> list[dict]:
    out: list[dict] = []
    for item in payload.get("articles") or []:
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        if not title or not url:
            continue
        seen = str(item.get("seendate") or "")
        try:
            published = datetime.strptime(seen, "%Y%m%dT%H%M%SZ").replace(
                tzinfo=UTC
            )
        except ValueError:
            published = datetime.now(UTC)
        out.append(
            {
                "title": title,
                "url": url,
                "summary": "",
                "published_at": published,
                "hash": article_hash(title, url),
                "domain": str(item.get("domain") or ""),
            }
        )
    return out


def _source_id(conn) -> int:
    row = conn.execute(
        """
        INSERT INTO news_sources (name, kind, url, credibility)
        VALUES ('gdelt', 'api', %s, 0.5)
        ON CONFLICT (name) DO UPDATE SET url = EXCLUDED.url
        RETURNING id
        """,
        (BASE,),
    ).fetchone()
    return int(row[0])


def backfill(
    days: int = 365,
    companies: int = 50,
    per_company: int = 250,
    pause: float = 5.0,
) -> dict:
    end = datetime.now(UTC)
    start = end - timedelta(days=days)
    with pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT c.id, c.name, array_agg(a.alias) AS aliases
            FROM companies c
            LEFT JOIN aliases a ON a.company_id = c.id
            WHERE c.name IS NOT NULL AND length(c.name) > 3
            GROUP BY c.id, c.name
            ORDER BY c.id
            LIMIT %s
            """,
            (companies,),
        ).fetchall()

    inserted = 0
    for _company_id, name, aliases in rows:
        terms = [alias for alias in (aliases or []) if alias and len(alias) > 3][:3]
        if not terms:
            continue
        query = " OR ".join(f'"{term}"' for term in terms) + " sourcelang:russian"
        params = {
            "query": query,
            "mode": "artlist",
            "maxrecords": per_company,
            "format": "json",
            "startdatetime": start.strftime("%Y%m%d%H%M%S"),
            "enddatetime": end.strftime("%Y%m%d%H%M%S"),
        }
        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.get(BASE, params=params)
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:  # noqa: BLE001 - one company must not break
            logger.warning("gdelt %s: %s", name, exc)
            time.sleep(pause)
            continue

        articles = parse_articles(payload)
        with pool().connection() as conn:
            source_id = _source_id(conn)
            for item in articles:
                row = conn.execute(
                    """
                    INSERT INTO news_articles
                        (source_id, url, title, summary, published_at,
                         lang, hash, raw)
                    VALUES (%s, %s, %s, %s, %s, 'ru', %s, %s)
                    ON CONFLICT (hash) DO NOTHING
                    RETURNING id
                    """,
                    (
                        source_id,
                        item["url"],
                        item["title"],
                        item["summary"],
                        item["published_at"],
                        item["hash"],
                        {"domain": item["domain"]},
                    ),
                ).fetchone()
                if row:
                    inserted += 1
            conn.commit()
        logger.info("gdelt %s: %d articles", name, len(articles))
        time.sleep(pause)

    from .link import link_pending

    linked = link_pending(limit=5000)
    return {"companies": len(rows), "inserted": inserted, "linked": linked}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="GDELT backfill for news")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--companies", type=int, default=50)
    parser.add_argument("--per-company", type=int, default=250)
    parser.add_argument("--pause", type=float, default=5.0)
    args = parser.parse_args()
    print(
        backfill(
            days=args.days,
            companies=args.companies,
            per_company=args.per_company,
            pause=args.pause,
        )
    )
