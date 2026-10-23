from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime

import feedparser
import httpx
from dateutil import parser as date_parser

from ..db import pool
from .sources import SOURCES

logger = logging.getLogger(__name__)


def article_hash(title: str, url: str) -> str:
    normalized = " ".join((title or "").lower().split())
    return hashlib.sha256(f"{normalized}|{url}".encode()).hexdigest()


def _published(entry) -> datetime:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        return datetime(*parsed[:6], tzinfo=UTC)
    raw = entry.get("published") or entry.get("updated")
    if raw:
        try:
            value = date_parser.parse(raw)
            return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            pass
    return datetime.now(UTC)


def parse_feed(data: bytes) -> list[dict]:
    feed = feedparser.parse(data)
    out: list[dict] = []
    for entry in feed.entries:
        title = (entry.get("title") or "").strip()
        url = (entry.get("link") or "").strip()
        if not title:
            continue
        summary = (entry.get("summary") or entry.get("description") or "").strip()
        out.append(
            {
                "title": title,
                "url": url,
                "summary": summary,
                "published_at": _published(entry),
                "hash": article_hash(title, url),
            }
        )
    return out


def _source_id(conn, source: dict) -> int:
    row = conn.execute(
        """
        INSERT INTO news_sources (name, kind, url, credibility)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (name) DO UPDATE SET
            url = EXCLUDED.url, kind = EXCLUDED.kind
        RETURNING id
        """,
        (
            source["name"],
            source["kind"],
            source.get("url"),
            source.get("credibility", 0.5),
        ),
    ).fetchone()
    return int(row[0])


def ingest_source(source: dict, client: httpx.Client | None = None) -> int:
    url = source.get("url")
    if not url:
        return 0
    own = client is None
    client = client or httpx.Client(timeout=20.0, follow_redirects=True)
    try:
        response = client.get(url)
        response.raise_for_status()
        items = parse_feed(response.content)
    finally:
        if own:
            client.close()

    inserted = 0
    with pool().connection() as conn:
        source_id = _source_id(conn, source)
        for item in items:
            row = conn.execute(
                """
                INSERT INTO news_articles
                    (source_id, url, title, summary, published_at, lang, hash)
                VALUES (%s, %s, %s, %s, %s, 'ru', %s)
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
                ),
            ).fetchone()
            if row:
                inserted += 1
        conn.commit()
    return inserted


def ingest_all() -> dict:
    total = 0
    with httpx.Client(timeout=20.0, follow_redirects=True) as client:
        for source in SOURCES:
            try:
                count = ingest_source(source, client)
                total += count
                logger.info("news %s: +%d", source["name"], count)
            except Exception as exc:  # noqa: BLE001 - one feed must not break the loop
                logger.warning("news %s failed: %s", source["name"], exc)
    return {"inserted": total}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(ingest_all())
