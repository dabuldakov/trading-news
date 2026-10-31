"""Telegram public channels via t.me/s (no API keys).

Публичный веб-превью канала отдаёт последние сообщения; этого достаточно для
быстрого потока новостей. Дедуп — по hash, как у RSS/GDELT.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import httpx
from bs4 import BeautifulSoup

from ..config import settings
from ..db import pool
from .ingest import article_hash

logger = logging.getLogger(__name__)

CRYPTO_CHANNELS: list[tuple[str, str]] = [
    ("forklog", "ForkLog"),
    ("incrypted", "Incrypted"),
    ("unfolded", "Unfolded"),
    ("decenter", "DeCenter"),
    ("cryp2day", "Cryp2day"),
    ("defimillion", "DeFi Million"),
    ("investigations", "ZachXBT"),
    ("binance_announcements", "Binance Announcements"),
    ("WatcherGuru", "Watcher Guru"),
    ("whale_alert_io", "Whale Alert"),
    ("wublockchainenglish", "Wu Blockchain News"),
    ("CoinMarketCapAnnouncements", "CoinMarketCap Announcements"),
    ("Bybit_Announcements", "Bybit Announcements"),
]

RUSSIAN_CHANNELS: list[tuple[str, str]] = [
    ("markettwits", "MarketTwits"),
    ("rbc_news", "РБК"),
    ("vedomosti", "Ведомости"),
    ("kommersant", "Коммерсантъ"),
    ("minpromtorg_ru", "Минпромторг России"),
    ("rostecru", "Ростех"),
    ("mash_tech", "МашТех"),
    ("eprussia", "Энергетика и промышленность России"),
    ("government_rus", "Правительство России"),
    ("minec_russia", "Минэкономразвития России"),
    ("MoscowEcon", "Экономика Москвы"),
    ("minfin", "Минфин России"),
    ("centralbank_russia", "Банк России"),
]

CHANNELS: list[tuple[str, str]] = (
    CRYPTO_CHANNELS if settings.news_profile == "crypto" else RUSSIAN_CHANNELS
)
CHANNEL_LANG = {
    "investigations": "en",
    "binance_announcements": "en",
    "WatcherGuru": "en",
    "whale_alert_io": "en",
    "wublockchainenglish": "en",
    "CoinMarketCapAnnouncements": "en",
    "Bybit_Announcements": "en",
}


def parse_channel(html: str, channel: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    for message in soup.select(".tgme_widget_message"):
        text_el = message.select_one(".tgme_widget_message_text")
        time_el = message.select_one("time")
        if text_el is None or time_el is None:
            continue
        title = " ".join(text_el.get_text(" ", strip=True).split())
        if not title:
            continue
        raw_time = str(time_el.get("datetime") or "")
        try:
            published = datetime.fromisoformat(raw_time).astimezone(UTC)
        except ValueError:
            published = datetime.now(UTC)
        post = str(message.get("data-post") or f"{channel}/0")
        url = f"https://t.me/{post}"
        out.append(
            {
                "title": title,
                "url": url,
                "summary": "",
                "published_at": published,
                "hash": article_hash(title, url),
            }
        )
    return out


def _source_id(conn, channel: str) -> int:
    name = f"telegram:{channel}"
    row = conn.execute(
        """
        INSERT INTO news_sources (name, kind, url, credibility)
        VALUES (%s, 'telegram', %s, 0.7)
        ON CONFLICT (name) DO UPDATE SET url = EXCLUDED.url
        RETURNING id
        """,
        (name, f"https://t.me/{channel}"),
    ).fetchone()
    return int(row[0])


def ingest_telegram() -> dict:
    inserted = 0
    with httpx.Client(
        timeout=20.0,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (compatible; trading-news/0.1)"},
    ) as client:
        for channel, _title in CHANNELS:
            try:
                response = client.get(f"https://t.me/s/{channel}")
                response.raise_for_status()
                items = parse_channel(response.text, channel)
            except Exception as exc:  # noqa: BLE001 - one channel must not break
                logger.warning("telegram %s: %s", channel, exc)
                continue
            with pool().connection() as conn:
                source_id = _source_id(conn, channel)
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
                            CHANNEL_LANG.get(channel, "ru"),
                            item["hash"],
                        ),
                    ).fetchone()
                    if row:
                        inserted += 1
                conn.commit()
            logger.info("telegram %s: %d messages", channel, len(items))

    from .link import link_pending

    linked = link_pending(limit=2000)
    return {"inserted": inserted, "linked": linked}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(ingest_telegram())
