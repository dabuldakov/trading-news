from __future__ import annotations

from trading_news.news.ingest import article_hash, parse_feed

RSS = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0">
  <channel>
    <title>Test</title>
    <item>
      <title>Сбербанк выплатит дивиденды</title>
      <link>https://example.com/a</link>
      <description>Совет директоров рекомендовал дивиденды.</description>
      <pubDate>Thu, 23 Oct 2026 10:00:00 +0300</pubDate>
    </item>
  </channel>
</rss>""".encode()


def test_parse_feed_extracts_item() -> None:
    items = parse_feed(RSS)
    assert len(items) == 1
    item = items[0]
    assert item["title"] == "Сбербанк выплатит дивиденды"
    assert item["url"] == "https://example.com/a"
    assert item["published_at"].isoformat() == "2026-10-23T07:00:00+00:00"
    assert item["hash"] == article_hash(item["title"], item["url"])


def test_article_hash_normalizes_title() -> None:
    assert article_hash("Сбербанк  выплатит", "u") == article_hash(
        "  сбербанк выплатит ", "u"
    )
    assert article_hash("A", "u") != article_hash("A", "v")
