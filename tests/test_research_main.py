from __future__ import annotations

from datetime import date, datetime, timedelta

from trading_news.db import pool
from trading_news.research import (
    clean_signals,
    filter_variants,
    link_eval,
    sector_flow,
    sector_paper,
)


def _seed_rows() -> None:
    with pool().connection() as conn:
        source = conn.execute(
            "INSERT INTO news_sources (name,kind) VALUES ('rbc','rss') RETURNING id"
        ).fetchone()[0]
        company = conn.execute(
            "INSERT INTO companies (name) VALUES ('Сбербанк') RETURNING id"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO instruments (ticker,secid,company_id) VALUES ('SBER','SBER',%s)",
            (company,),
        )
        for i in range(3):
            article = conn.execute(
                """
                INSERT INTO news_articles (source_id,title,published_at,hash)
                VALUES (%s,'Сбербанк',%s,md5(%s)) RETURNING id
                """,
                (source, datetime.now() - timedelta(days=i), f"x{i}"),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO news_analysis (article_id,event_type,sentiment) "
                "VALUES (%s,'dividend',1)",
                (article,),
            )
            conn.execute(
                "INSERT INTO news_tickers (article_id,ticker,confidence,method,relevance) "
                "VALUES (%s,'SBER',0.95,'exact',1.0)",
                (article,),
            )
            conn.execute(
                "INSERT INTO news_reactions (article_id,ticker,t0,abnormal_ret,abnormal_3d,abnormal_5d) "
                "VALUES (%s,'SBER',now(),0.01,0.02,0.03)",
                (article,),
            )
        conn.commit()


def test_link_eval_empty() -> None:
    result = link_eval.evaluate()
    assert result["n"] == len(link_eval.CASES)
    assert "precision" in result["rules"]


def test_filter_and_clean_main(monkeypatch, capsys) -> None:
    _seed_rows()
    monkeypatch.setattr(filter_variants, "TICKERS", [5])
    monkeypatch.setattr(filter_variants, "LINKS", [0.9])
    filter_variants.main()
    assert "max_t link" in capsys.readouterr().out

    monkeypatch.setattr(clean_signals, "HORIZONS", {"1d": "abnormal_ret"})
    clean_signals.main()
    assert "clean_rows" in capsys.readouterr().out


def test_sector_build_and_paper(monkeypatch) -> None:

    monkeypatch.setattr(sector_flow, "_active_tickers", lambda: {"tech": ["SBER"]})
    monkeypatch.setattr(
        sector_flow,
        "_closes",
        lambda ticker, start, end: {
            date(2026, 1, 1): 100.0,
            date(2026, 1, 2): 101.0,
        },
    )
    monkeypatch.setattr(
        sector_flow,
        "_market",
        lambda start, end: [
            {"begin": datetime(2026, 1, 1), "close": 100.0},
            {"begin": datetime(2026, 1, 2), "close": 100.5},
        ],
    )
    assert sector_flow.build_sector_returns("2026-01-01", "2026-01-02") >= 1
    assert sector_paper.paper_update(horizon=1) == 0
    monkeypatch.setattr(sector_paper, "_load_returns", lambda a, b: {})
    monkeypatch.setattr(sector_paper, "_load_flow", lambda a, b: {})
    assert sector_paper.paper_capture(days=1, horizon=1) == 0


def test_worker_main(monkeypatch) -> None:
    from trading_news import worker

    class Scheduler:
        def start(self):
            return None

    monkeypatch.setattr(worker, "migrate", lambda: [])
    monkeypatch.setattr(worker, "build_scheduler", lambda: Scheduler())
    monkeypatch.setattr(worker, "close_pool", lambda: None)
    worker.main()
