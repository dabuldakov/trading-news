from __future__ import annotations

from trading_news.news.gdelt import parse_articles


def test_parse_articles() -> None:
    payload = {
        "articles": [
            {
                "title": "Сбербанк выплатит дивиденды",
                "url": "https://example.com/a",
                "seendate": "20261023T071500Z",
                "domain": "rbc.ru",
            },
            {"title": "", "url": "https://example.com/b"},
        ]
    }
    items = parse_articles(payload)
    assert len(items) == 1
    assert items[0]["title"] == "Сбербанк выплатит дивиденды"
    assert items[0]["published_at"].isoformat() == "2026-10-23T07:15:00+00:00"
    assert items[0]["domain"] == "rbc.ru"
    assert len(items[0]["hash"]) == 64
