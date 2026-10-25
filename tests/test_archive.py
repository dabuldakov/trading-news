from __future__ import annotations

from datetime import datetime

from trading_news.db import pool
from trading_news.news import archive

INDEX = b"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://www.vedomosti.ru/sitemap_news3.xml</loc></sitemap>
</sitemapindex>"""

URLSET = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.vedomosti.ru/business/news/2025/01/10/1-test</loc>
       <lastmod>2025-01-10T10:00:00+03:00</lastmod></url>
  <url><loc>https://www.vedomosti.ru/sport/news/2025/01/10/2-sport</loc>
       <lastmod>2025-01-10T10:00:00+03:00</lastmod></url>
</urlset>"""

ARTICLE = """<html><head>
<meta property="og:title" content="Компания отчиталась о прибыли - Ведомости" />
<meta name="description" content="Краткий лид статьи." />
<meta property="article:published_time" content="2025-01-10T10:05:00+03:00" />
</head><body></body></html>""".encode()


def test_parse_sitemap_index_and_urlset() -> None:
    kind, entries = archive.parse_sitemap(INDEX)
    assert kind == "sitemapindex"
    assert entries[0]["loc"].endswith("sitemap_news3.xml")
    kind, entries = archive.parse_sitemap(URLSET)
    assert kind == "urlset"
    assert len(entries) == 2


def test_parse_article_extracts_title_and_summary() -> None:
    item = archive.parse_article(ARTICLE, "https://x/1")
    assert item is not None
    assert item["title"] == "Компания отчиталась о прибыли"
    assert item["summary"] == "Краткий лид статьи."
    assert item["published"] == datetime.fromisoformat("2025-01-10T07:05:00+00:00")


def test_match_filters_sections() -> None:
    source = archive.SOURCES["vedomosti"]
    assert archive._match("https://www.vedomosti.ru/business/news/1", source)
    assert not archive._match("https://www.vedomosti.ru/sport/news/1", source)


def test_scrape_inserts_article(monkeypatch) -> None:
    def fake_fetch(url: str) -> bytes:
        if url.endswith("sitemap.xml"):
            return INDEX
        if url.endswith("sitemap_news3.xml"):
            return URLSET
        return ARTICLE

    monkeypatch.setattr(archive, "_fetch", fake_fetch)
    result = archive.scrape(
        "vedomosti", "2025-01-01", "2025-01-31", limit=10, workers=2, pause=0
    )
    assert result["inserted"] == 1
    with pool().connection() as conn:
        row = conn.execute(
            "SELECT title, url FROM news_articles ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row[0] == "Компания отчиталась о прибыли"
    assert row[1].endswith("/1-test")
