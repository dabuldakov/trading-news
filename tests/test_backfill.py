from __future__ import annotations

import httpx
import respx

from trading_news.news import gdelt, wayback


@respx.mock
def test_wayback_backfill(monkeypatch) -> None:
    monkeypatch.setattr(
        wayback,
        "SOURCES",
        [{"name": "test", "kind": "rss", "url": "https://feed.test/rss"}],
    )
    respx.get("http://web.archive.org/cdx/search/cdx").mock(
        return_value=httpx.Response(
            200,
            json=[
                ["urlkey", "timestamp", "original", "statuscode"],
                ["k", "20251005153120", "https://feed.test/rss", "200"],
            ],
        )
    )
    respx.get(
        "https://web.archive.org/web/20251005153120id_/https://feed.test/rss"
    ).mock(
        return_value=httpx.Response(
            200,
            content=(
                b"<?xml version='1.0'?><rss><channel><item>"
                b"<title>Bitcoin</title><link>https://x/1</link>"
                b"<pubDate>Fri, 30 Oct 2026 10:00:00 GMT</pubDate>"
                b"</item></channel></rss>"
            ),
        )
    )
    result = wayback.backfill_wayback(days=1, pause=0)
    assert result["snapshots"] == 1
    assert result["inserted"] == 1


@respx.mock
def test_gdelt_backfill(monkeypatch) -> None:

    from trading_news.db import pool

    monkeypatch.setattr(gdelt.time, "sleep", lambda _x: None)
    with pool().connection() as conn:
        company = conn.execute(
            "INSERT INTO companies (name) VALUES ('Сбербанк') RETURNING id"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO aliases (company_id, alias, kind) VALUES (%s,'Сбербанк','short')",
            (company,),
        )
        conn.commit()
    respx.get("https://api.gdeltproject.org/api/v2/doc/doc").mock(
        return_value=httpx.Response(
            200,
            json={
                "articles": [
                    {
                        "title": "Сбербанк: новость",
                        "url": "https://x/gdelt",
                        "seendate": "20261030T100000Z",
                    }
                ]
            },
        )
    )
    result = gdelt.backfill(days=1, companies=1, pause=0)
    assert result["companies"] == 1
    assert result["inserted"] == 1
