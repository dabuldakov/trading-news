from __future__ import annotations

from datetime import date, datetime

import pytest

from trading_news.db import pool
from trading_news.research import sector_flow
from trading_news.research.sector_flow import (
    MARKET,
    _daily_returns,
    _load_flow,
    analyze,
    build_sector_returns,
    forward_records,
    spearman,
)


def test_daily_returns_equal_weight() -> None:
    d1, d2, d3 = date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)
    series = {
        "A": {d1: 100.0, d2: 110.0, d3: 121.0},
        "B": {d1: 100.0, d2: 100.0, d3: 90.0},
    }
    result = _daily_returns(series)
    assert result[d2] == pytest.approx(0.05)
    assert result[d3] == pytest.approx(0.0)


def test_spearman_handles_order_and_ties() -> None:
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert spearman([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0)
    assert spearman([1, 1, 2], [1, 2, 2]) == pytest.approx(0.5)
    assert spearman([1, 2], [1, 2]) == 0.0


def test_forward_records_compounds_future_days() -> None:
    d1, d2, d3 = date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)
    returns = {
        (d1, MARKET): 0.0,
        (d2, MARKET): 0.0,
        (d3, MARKET): 0.0,
        (d1, "oil_gas"): 0.0,
        (d2, "oil_gas"): 0.01,
        (d3, "oil_gas"): 0.02,
    }
    flow = {(d1, "oil_gas"): {"sentiment": 1.0, "n": 2, "net": 1.0}}
    records = forward_records(returns, flow, horizon=2)
    assert len(records) == 1
    assert records[0]["ret"] == pytest.approx(1.01 * 1.02 - 1)
    assert records[0]["excess"] == pytest.approx(1.01 * 1.02 - 1)


def test_analyze_detects_cross_sectional_signal() -> None:
    records = []
    for index in range(10):
        day = date(2026, 1, 1 + index)
        for rank, sentiment in enumerate((-0.3, -0.1, 0.1, 0.3)):
            excess = sentiment * 0.01
            records.append(
                {
                    "day": day,
                    "sector": f"s{rank}",
                    "sentiment": sentiment,
                    "net": sentiment,
                    "n": 2,
                    "ret": excess,
                    "excess": excess,
                }
            )
    result = analyze(records, top=1)
    assert result["ic_sentiment"]["mean"] == pytest.approx(1.0)
    assert result["buckets"]["spread"]["mean"] > 0
    assert result["long_short"]["mean"] > 0
    assert result["observations"] == 40
    assert set(result["by_sector"]) == {"s0", "s1", "s2", "s3"}


def test_load_flow_dedups_articles_per_sector() -> None:
    with pool().connection() as conn:
        source = conn.execute(
            "INSERT INTO news_sources (name, kind) VALUES ('rbc', 'rss') RETURNING id"
        ).fetchone()[0]
        company = conn.execute(
            "INSERT INTO companies (name, sector) "
            "VALUES ('Газпром', 'oil_gas') RETURNING id"
        ).fetchone()[0]
        for ticker in ("GAZP", "ROSN"):
            conn.execute(
                "INSERT INTO instruments (ticker, secid, company_id, status) "
                "VALUES (%s, %s, %s, 'active')",
                (ticker, ticker, company),
            )
        article = conn.execute(
            """
            INSERT INTO news_articles (source_id, title, published_at, hash)
            VALUES (%s, 'Газпром и Роснефть', '2026-01-05 10:00+03', md5('x'))
            RETURNING id
            """,
            (source,),
        ).fetchone()[0]
        for ticker in ("GAZP", "ROSN"):
            conn.execute(
                "INSERT INTO news_tickers (article_id, ticker) VALUES (%s, %s)",
                (article, ticker),
            )
        conn.execute(
            "INSERT INTO news_analysis (article_id, sentiment, event_type) "
            "VALUES (%s, 1, 'other')",
            (article,),
        )
        conn.commit()

    flow = _load_flow("2026-01-01", "2026-01-10")
    value = flow[(date(2026, 1, 5), "oil_gas")]
    assert value["n"] == 1
    assert value["sentiment"] == pytest.approx(1.0)


def test_build_sector_returns_stores_baskets(monkeypatch) -> None:
    d1, d2 = date(2026, 1, 1), date(2026, 1, 2)
    closes = {
        "GAZP": {d1: 100.0, d2: 110.0},
        "ROSN": {d1: 100.0, d2: 100.0},
    }
    monkeypatch.setattr(
        sector_flow, "_closes", lambda ticker, start, end: closes[ticker]
    )
    monkeypatch.setattr(
        sector_flow,
        "_market",
        lambda start, end: [
            {"begin": datetime(2026, 1, 1), "close": 100.0},
            {"begin": datetime(2026, 1, 2), "close": 102.0},
        ],
    )
    with pool().connection() as conn:
        company = conn.execute(
            "INSERT INTO companies (name, sector) "
            "VALUES ('Газпром', 'oil_gas') RETURNING id"
        ).fetchone()[0]
        for ticker in ("GAZP", "ROSN"):
            conn.execute(
                "INSERT INTO instruments (ticker, secid, company_id, status) "
                "VALUES (%s, %s, %s, 'active')",
                (ticker, ticker, company),
            )
        conn.commit()

    assert build_sector_returns("2026-01-01", "2026-01-10") == 2
    with pool().connection() as conn:
        rows = dict(
            conn.execute(
                "SELECT sector, ret_1d FROM sector_returns WHERE day = %s",
                (d2,),
            ).fetchall()
        )
    assert rows["oil_gas"] == pytest.approx(0.05)
    assert rows[MARKET] == pytest.approx(0.02)
