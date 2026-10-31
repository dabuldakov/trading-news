"""Исторический бэкфилл RSS через Wayback Machine.

Wayback CDX отдаёт по одному снимку в день для RSS; скачиваем снимок
(``id_`` — сырой контент) и парсим тем же ``parse_feed``, что и живой ingest.
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import UTC, datetime, timedelta

import httpx

from ..db import pool
from .ingest import parse_feed
from .sources import SOURCES

logger = logging.getLogger(__name__)

CDX_API = "http://web.archive.org/cdx/search/cdx"
SNAPSHOT = "https://web.archive.org/web/{timestamp}id_/{original}"


def cdx_snapshots(url: str, start: str, end: str, limit: int = 500) -> list[dict]:
    params = {
        "url": url,
        "output": "json",
        "from": start,
        "to": end,
        "filter": "statuscode:200",
        "collapse": "timestamp:8",
        "limit": limit,
    }
    with httpx.Client(timeout=40.0) as client:
        response = client.get(CDX_API, params=params)
        response.raise_for_status()
        data = response.json()
    if not data:
        return []
    header, *rows = data
    index = {name: i for i, name in enumerate(header)}
    out: list[dict] = []
    for row in rows:
        if row[index["statuscode"]] != "200":
            continue
        out.append(
            {
                "timestamp": row[index["timestamp"]],
                "original": row[index["original"]],
            }
        )
    return out


def fetch_snapshot(snapshot: dict) -> bytes:
    url = SNAPSHOT.format(**snapshot)
    with httpx.Client(timeout=40.0, follow_redirects=True) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.content


def _source_id(conn, name: str, url: str) -> int:
    row = conn.execute(
        """
        INSERT INTO news_sources (name, kind, url, credibility)
        VALUES (%s, 'wayback', %s, 0.6)
        ON CONFLICT (name) DO UPDATE SET url = EXCLUDED.url
        RETURNING id
        """,
        (f"wayback:{name}", url),
    ).fetchone()
    return int(row[0])


def backfill_wayback(
    days: int = 365,
    limit_per_source: int = 120,
    pause: float = 1.0,
    only: set[str] | None = None,
) -> dict:
    end = datetime.now(UTC)
    start = end - timedelta(days=days)
    sources = [
        source
        for source in SOURCES
        if source.get("url")
        and source.get("kind") == "rss"
        and (not only or source["name"] in only)
    ]
    inserted = 0
    snapshots_total = 0
    for source in sources:
        snapshots = cdx_snapshots(
            source["url"],
            start.strftime("%Y%m%d"),
            end.strftime("%Y%m%d"),
            limit=limit_per_source,
        )
        snapshots_total += len(snapshots)
        logger.info("wayback %s: %d snapshots", source["name"], len(snapshots))
        for snapshot in snapshots:
            try:
                items = parse_feed(fetch_snapshot(snapshot))
            except Exception as exc:  # noqa: BLE001 - один снимок не рвёт цикл
                logger.warning("wayback %s %s: %s", source["name"], snapshot, exc)
                time.sleep(pause)
                continue
            with pool().connection() as conn:
                source_id = _source_id(conn, source["name"], source["url"])
                for item in items:
                    row = conn.execute(
                        """
                        INSERT INTO news_articles
                            (source_id, url, title, summary, published_at,
                             lang, hash)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (hash) DO NOTHING
                        RETURNING id
                        """,
                        (
                            source_id,
                            item["url"],
                            item["title"],
                            item["summary"],
                            item["published_at"],
                            source.get("lang", "ru"),
                            item["hash"],
                        ),
                    ).fetchone()
                    if row:
                        inserted += 1
                conn.commit()
            time.sleep(pause)

    from .link import link_pending

    linked = link_pending(limit=5000)
    return {
        "sources": len(sources),
        "snapshots": snapshots_total,
        "inserted": inserted,
        "linked": linked,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="Wayback RSS backfill")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--limit", type=int, default=120)
    parser.add_argument("--pause", type=float, default=1.0)
    parser.add_argument("--only", default="")
    args = parser.parse_args()
    only = {part.strip() for part in args.only.split(",") if part.strip()} or None
    print(
        backfill_wayback(
            days=args.days,
            limit_per_source=args.limit,
            pause=args.pause,
            only=only,
        )
    )
