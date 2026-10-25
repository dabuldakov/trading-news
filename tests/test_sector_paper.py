from __future__ import annotations

from datetime import date, timedelta

import pytest

from trading_news.db import pool
from trading_news.research.sector_paper import (
    paper_capture,
    paper_replay,
    paper_update,
)


def _seed_day(conn, day: date, sentiment: float = -1.0) -> None:
    source = conn.execute(
        "INSERT INTO news_sources (name, kind) "
        "VALUES ('paper-rbc', 'rss') RETURNING id"
    ).fetchone()[0]
    company = conn.execute(
        "INSERT INTO companies (name, sector) "
        "VALUES ('Газпром', 'oil_gas') RETURNING id"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO instruments (ticker, secid, company_id, status) "
        "VALUES ('GAZP', 'GAZP', %s, 'active')",
        (company,),
    )
    article = conn.execute(
        """
        INSERT INTO news_articles (source_id, title, published_at, hash)
        VALUES (%s, 'Газпром: негатив', %s, md5('paper'))
        RETURNING id
        """,
        (source, f"{day.isoformat()} 10:00+03"),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO news_tickers (article_id, ticker) VALUES (%s, 'GAZP')",
        (article,),
    )
    conn.execute(
        "INSERT INTO news_analysis (article_id, sentiment, event_type) "
        "VALUES (%s, %s, 'other')",
        (article, sentiment),
    )


def test_paper_capture_update_and_replay() -> None:
    today = date.today()
    signal_day = today - timedelta(days=4)
    entry_day = today - timedelta(days=3)
    with pool().connection() as conn:
        _seed_day(conn, signal_day)
        for day, sector, value in (
            (signal_day, "oil_gas", 0.0),
            (entry_day, "oil_gas", 0.02),
            (signal_day, "market", 0.0),
            (entry_day, "market", 0.01),
        ):
            conn.execute(
                "INSERT INTO sector_returns (day, sector, ret_1d, n_tickers) "
                "VALUES (%s, %s, %s, 1)",
                (day, sector if sector != "market" else "__market__", value),
            )
        conn.commit()

    assert paper_capture(days=7) == 1
    assert paper_capture(days=7) == 1
    with pool().connection() as conn:
        row = conn.execute(
            "SELECT entry_day, status FROM sector_paper_trades"
        ).fetchone()
    assert row[0] == entry_day
    assert row[1] == "open"

    assert paper_update() == 1
    with pool().connection() as conn:
        row = conn.execute(
            "SELECT status, excess FROM sector_paper_trades"
        ).fetchone()
    assert row[0] == "closed"
    assert row[1] == pytest.approx(0.01)

    replay = paper_replay(
        signal_day.isoformat(),
        today.isoformat(),
        horizon=1,
    )
    assert replay["trades"] == 1
    assert replay["mean"] == pytest.approx(0.01 - 0.001)
    assert replay["hit"] == pytest.approx(1.0)
