from __future__ import annotations

import json
from datetime import UTC, datetime

from trading_news.db import pool
from trading_news.news import gdelt_bq

ARTICLE = """<html><head>
<meta property="og:title" content="РБК: рынок вырос" />
<meta name="description" content="Лид." />
<meta property="article:published_time" content="2024-01-10T10:05:00+03:00" />
</head><body></body></html>""".encode()


def test_build_query_validates_domains() -> None:
    sql = gdelt_bq.build_query("2024-01-01", "2024-02-01", ["rbc.ru"])
    assert "gkg_partitioned" in sql
    assert "2024-01-01" in sql and "'rbc.ru'" in sql
    try:
        gdelt_bq.build_query("2024-01-01", "2024-02-01", ["bad domain!"])
    except ValueError:
        pass
    else:
        raise AssertionError("bad domain must raise")


def test_parse_row() -> None:
    item = gdelt_bq.parse_row(
        {
            "DATE": "20240110123000",
            "SourceCommonName": "rbc.ru",
            "DocumentIdentifier": "https://www.rbc.ru/x",
            "V2Tone": "-1.2,3.4",
            "Themes": "TAX_FNCACT",
        }
    )
    assert item is not None
    assert item["published_at"] == "2024-01-10T12:30:00+00:00"
    assert item["tone"] == -1.2
    assert item["themes"] == "TAX_FNCACT"
    assert gdelt_bq.parse_row({"DATE": "bad"}) is None


def test_load_and_enrich(tmp_path, monkeypatch) -> None:
    payload = {
        "url": "https://www.rbc.ru/business/10/01/2024/abc",
        "published_at": "2024-01-10T07:00:00+00:00",
        "domain": "rbc.ru",
        "tone": 0.5,
        "themes": "ECON",
    }
    path = tmp_path / "gdelt.jsonl"
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    assert gdelt_bq.load_jsonl(str(path)) == 1
    assert gdelt_bq.load_jsonl(str(path)) == 0

    monkeypatch.setattr(gdelt_bq, "_fetch", lambda url: ARTICLE)
    result = gdelt_bq.enrich(10)
    assert result["inserted"] == 1
    with pool().connection() as conn:
        article = conn.execute(
            "SELECT title, published_at FROM news_articles LIMIT 1"
        ).fetchone()
        resolved = conn.execute(
            "SELECT resolved FROM gdelt_documents LIMIT 1"
        ).fetchone()[0]
    assert article[0] == "РБК: рынок вырос"
    assert article[1] == datetime(2024, 1, 10, 7, 5, tzinfo=UTC)
    assert resolved is True
