from __future__ import annotations

from datetime import datetime

import pytest

from trading_news.db import pool
from trading_news.research.event_study import (
    _reaction,
    build_markers,
    build_signals,
)
from trading_news.research.stats import event_group, source_key


def _bar(day: int, close: float, high: float | None = None, low: float | None = None):
    return {
        "begin": datetime(2026, 1, day),
        "open": close,
        "high": high if high is not None else close,
        "low": low if low is not None else close,
        "close": close,
        "volume": 1000.0,
    }


def test_reaction_positive_day() -> None:
    bars = [
        _bar(1, 100.0),
        _bar(2, 100.0),
        _bar(3, 105.0, high=106.0),
        _bar(4, 108.0),
        _bar(5, 110.0),
    ]
    result = _reaction(bars, bars, datetime(2026, 1, 3))
    assert result is not None
    assert result["t0"] == datetime(2026, 1, 3)
    assert result["ret_1d"] == pytest.approx(0.05)
    assert result["ret_3d"] == pytest.approx(0.10)
    assert result["mfe"] == pytest.approx(0.10)


def test_reaction_without_future_bars() -> None:
    bars = [_bar(1, 100.0), _bar(2, 100.0)]
    assert _reaction(bars, bars, datetime(2026, 1, 10)) is None


def test_reaction_abnormal_vs_market() -> None:
    bars = [
        _bar(1, 100.0),
        _bar(2, 100.0),
        _bar(3, 102.0),
        _bar(4, 103.0),
        _bar(5, 104.0),
        _bar(6, 105.0),
    ]
    market = [
        _bar(1, 100.0),
        _bar(2, 100.0),
        _bar(3, 101.0),
        _bar(4, 101.5),
        _bar(5, 102.0),
        _bar(6, 102.5),
    ]
    result = _reaction(bars, market, datetime(2026, 1, 3))
    assert result is not None
    assert result["ret_1d"] == pytest.approx(0.02)
    assert result["abnormal_ret"] == pytest.approx(0.01)
    assert result["abnormal_3d"] == pytest.approx(0.04 - 0.02)


def test_source_key_and_event_group() -> None:
    assert source_key("wayback:rbc") == "rbc"
    assert source_key("rbc") == "rbc"
    assert event_group("dividend") == "corporate"
    assert event_group("rate") == "macro"
    assert event_group("weird") == "other"


def test_build_markers_and_signals() -> None:
    with pool().connection() as conn:
        source = conn.execute(
            "INSERT INTO news_sources (name, kind) "
            "VALUES ('rbc', 'rss') RETURNING id"
        ).fetchone()[0]
        company = conn.execute(
            "INSERT INTO companies (name) VALUES ('ПАО Сбербанк') RETURNING id"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO instruments (ticker, secid, company_id) "
            "VALUES ('SBER', 'SBER', %s)",
            (company,),
        )
        for index in range(5):
            article = conn.execute(
                """
                INSERT INTO news_articles (source_id, title, published_at, hash)
                VALUES (%s, 'Сбербанк выплатит дивиденды', now(), md5(%s))
                RETURNING id
                """,
                (source, str(index)),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO news_tickers (article_id, ticker) "
                "VALUES (%s, 'SBER')",
                (article,),
            )
            conn.execute(
                """
                INSERT INTO news_analysis (article_id, sentiment, event_type)
                VALUES (%s, 1, 'dividend')
                """,
                (article,),
            )
            value = (0.005, 0.008, 0.010, 0.012, 0.015)[index]
            conn.execute(
                """
                INSERT INTO news_reactions
                    (article_id, ticker, t0, abnormal_ret)
                VALUES (%s, 'SBER', now(), %s)
                """,
                (article, value),
            )
        conn.commit()

    assert build_markers() == 1
    assert build_signals() == 5

    with pool().connection() as conn:
        marker = conn.execute(
            "SELECT marker_key, n, mean FROM event_markers"
        ).fetchone()
        signal = conn.execute(
            "SELECT side, expected_edge, marker_key FROM signals LIMIT 1"
        ).fetchone()
    assert marker[0] == "dividend|pos|rbc"
    assert marker[1] == 5
    assert marker[2] == pytest.approx(0.01)
    assert signal[0] == "buy"
    assert signal[1] == pytest.approx(0.009)
