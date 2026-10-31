"""Backfill public Telegram channels through t.me/s paging.

The live collector only reads the latest page.  This script walks backwards
with ``?before=<post_id>`` until the cutoff date, with a small delay between
requests.  It writes through the same source/article tables and hash-based
deduplication as the live collector.
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import UTC, datetime, timedelta

import httpx

from ..db import pool
from .telegram import CHANNEL_LANG, CHANNELS, _source_id, parse_channel

logger = logging.getLogger(__name__)


def _post_id(url: str) -> int | None:
    value = (url or "").rsplit("/", 1)[-1]
    return int(value) if value.isdigit() else None


def backfill_channel(
    client: httpx.Client,
    channel: str,
    cutoff: datetime,
    *,
    delay: float = 1.2,
    max_pages: int = 2000,
) -> dict:
    before: int | None = None
    pages = 0
    fetched = inserted = 0
    oldest = None
    while pages < max_pages:
        url = f"https://t.me/s/{channel}"
        if before is not None:
            url += f"?before={before}"
        try:
            response = client.get(url)
            response.raise_for_status()
            items = parse_channel(response.text, channel)
        except Exception as exc:  # noqa: BLE001
            logger.warning("telegram backfill %s: %s", channel, exc)
            break
        if not items:
            break
        fetched += len(items)
        pages += 1
        with pool().connection() as conn:
            source_id = _source_id(conn, channel)
            for item in items:
                published = item["published_at"]
                if published < cutoff:
                    continue
                row = conn.execute(
                    """
                    INSERT INTO news_articles
                        (source_id, url, title, summary, published_at, lang, hash)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (hash) DO NOTHING
                    RETURNING id
                    """,
                    (
                        source_id,
                        item["url"],
                        item["title"],
                        item["summary"],
                        published,
                        CHANNEL_LANG.get(channel, "ru"),
                        item["hash"],
                    ),
                ).fetchone()
                inserted += bool(row)
            conn.commit()
        page_oldest = min(x["published_at"] for x in items)
        oldest = page_oldest
        print(
            f"{channel}: page {pages} n={len(items)} inserted={inserted} "
            f"oldest={page_oldest.isoformat()}",
            flush=True,
        )
        if page_oldest < cutoff:
            break
        ids = [_post_id(x["url"]) for x in items]
        ids = [x for x in ids if x is not None]
        if not ids:
            break
        new_before = min(ids)
        if before is not None and new_before >= before:
            break
        before = new_before
        time.sleep(delay)
    return {
        "channel": channel,
        "pages": pages,
        "fetched": fetched,
        "inserted": inserted,
        "oldest": oldest.isoformat() if oldest else None,
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="Telegram 90-day backfill")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--channels", default="", help="comma-separated; default=all")
    parser.add_argument("--delay", type=float, default=1.2)
    parser.add_argument("--max-pages", type=int, default=2000)
    args = parser.parse_args()
    cutoff = datetime.now(UTC) - timedelta(days=args.days)
    requested = {x.strip() for x in args.channels.split(",") if x.strip()}
    channels = [ch for ch, _ in CHANNELS if not requested or ch in requested]
    print("cutoff", cutoff.isoformat(), "channels", len(channels), flush=True)
    with httpx.Client(
        timeout=20.0,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (compatible; trading-news/0.1)"},
    ) as client:
        for channel in channels:
            print(
                backfill_channel(
                    client, channel, cutoff, delay=args.delay, max_pages=args.max_pages
                ),
                flush=True,
            )


if __name__ == "__main__":
    main()
