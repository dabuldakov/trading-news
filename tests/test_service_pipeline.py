from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from trading_news.db import pool
from trading_news.news import ingest, telegram, telegram_backfill
from trading_news.news.ingest import ingest_all
from trading_news.scheduler import build_scheduler


RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>
<item><title>Bitcoin rises</title><link>https://example.com/1</link>
<description>BTC test</description><pubDate>Fri, 30 Oct 2026 10:00:00 GMT</pubDate></item>
</channel></rss>"""

TG = """<html><body>
<div class="tgme_widget_message" data-post="testchan/10">
<div class="tgme_widget_message_text">Bitcoin rises</div>
<time datetime="2026-10-30T10:00:00+00:00"></time></div>
</body></html>"""


@respx.mock
def test_ingest_all_uses_feed(monkeypatch) -> None:
    monkeypatch.setattr(
        ingest,
        "SOURCES",
        [
            {
                "name": "testfeed",
                "kind": "rss",
                "url": "https://feed.test/rss",
                "market": "external",
                "lang": "en",
            }
        ],
    )
    respx.get("https://feed.test/rss").mock(
        return_value=httpx.Response(200, content=RSS)
    )
    result = ingest_all()
    assert result["inserted"] == 1
    assert result["skipped"] == 0


@respx.mock
def test_telegram_ingest_and_backfill(monkeypatch) -> None:
    monkeypatch.setattr(telegram, "CHANNELS", [("testchan", "Test")])
    respx.get("https://t.me/s/testchan").mock(
        return_value=httpx.Response(200, text=TG)
    )
    result = telegram.ingest_telegram()
    assert result["inserted"] == 1

    monkeypatch.setattr(telegram_backfill, "_source_id", lambda conn, ch: 1)
    with httpx.Client() as client:
        result = telegram_backfill.backfill_channel(
            client,
            "testchan",
            datetime.now(UTC) - timedelta(days=1),
            delay=0,
            max_pages=1,
        )
    assert result["fetched"] == 1
    assert result["pages"] == 1


def test_scheduler_has_jobs() -> None:
    scheduler = build_scheduler()
    assert scheduler.get_job("news") is not None
    assert scheduler.get_job("telegram") is not None
    assert scheduler.get_job("nlp") is not None
