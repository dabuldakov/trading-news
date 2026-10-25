from __future__ import annotations

from datetime import date, datetime

import pytest

from trading_news.db import pool
from trading_news.research.oos import (
    _add_months,
    evaluate,
    event_study,
    portfolio_backtest,
    walk_forward,
)


def _source(conn, name: str, credibility: float = 0.8) -> int:
    return conn.execute(
        """
        INSERT INTO news_sources (name, kind, credibility)
        VALUES (%s, 'rss', %s)
        ON CONFLICT (name) DO UPDATE SET credibility = EXCLUDED.credibility
        RETURNING id
        """,
        (name, credibility),
    ).fetchone()[0]


def _instrument(conn, ticker: str, sector: str = "banks") -> int:
    company = conn.execute(
        """
        INSERT INTO companies (name, sector) VALUES (%s, %s)
        ON CONFLICT (name) DO UPDATE SET sector = EXCLUDED.sector
        RETURNING id
        """,
        (f"{ticker} Co", sector),
    ).fetchone()[0]
    conn.execute(
        """
        INSERT INTO instruments (ticker, secid, company_id, status)
        VALUES (%s, %s, %s, 'active')
        ON CONFLICT (ticker) DO UPDATE SET company_id = EXCLUDED.company_id
        """,
        (ticker, ticker, company),
    )
    return company


def _article(
    conn,
    source_id: int,
    *,
    tag: str,
    ticker: str,
    published: datetime,
    event_type: str = "dividend",
    sentiment: float = 1.0,
    abnormal_ret: float | None = 0.02,
    ret_1d: float | None = 0.02,
    abnormal_3d: float | None = None,
    abnormal_5d: float | None = None,
    adv: float | None = 2_000_000.0,
    t0: datetime | None = None,
) -> int:
    article = conn.execute(
        """
        INSERT INTO news_articles (source_id, title, published_at, hash, status)
        VALUES (%s, %s, %s, md5(%s), 'linked')
        RETURNING id
        """,
        (source_id, tag, published, f"{tag}-{published.isoformat()}"),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO news_tickers (article_id, ticker) VALUES (%s, %s)",
        (article, ticker),
    )
    conn.execute(
        """
        INSERT INTO news_analysis (article_id, sentiment, event_type)
        VALUES (%s, %s, %s)
        """,
        (article, sentiment, event_type),
    )
    conn.execute(
        """
        INSERT INTO news_reactions
            (article_id, ticker, t0, ret_1d, abnormal_ret,
             abnormal_3d, abnormal_5d, adv_rub)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            article,
            ticker,
            t0 or published,
            ret_1d,
            abnormal_ret,
            abnormal_3d,
            abnormal_5d,
            adv,
        ),
    )
    return article


def _train_marker(conn, source_id: int, *, count: int = 10) -> None:
    for index in range(count):
        _article(
            conn,
            source_id,
            tag=f"train-{index}",
            ticker="SBER",
            published=datetime(2025, 12, 1),
            abnormal_ret=0.01,
            ret_1d=0.01,
        )


def test_evaluate_matches_wayback_marker_to_live_source() -> None:
    with pool().connection() as conn:
        backfill = _source(conn, "wayback:rbc", 0.8)
        live = _source(conn, "rbc", 0.8)
        _instrument(conn, "SBER")
        _train_marker(conn, backfill)
        for index in range(2):
            _article(
                conn,
                live,
                tag=f"test-{index}",
                ticker="SBER",
                published=datetime(2026, 1, 5 + index),
            )
        conn.commit()

    summary = evaluate(
        "2026-01-01",
        "2026-01-01",
        horizon=1,
        key_mode="source",
        min_marker_n=5,
        min_t=0.0,
        min_edge=0.0,
    )

    assert summary["markers_train"] == 1
    assert summary["pairs"] == 2
    assert summary["signals_oos"] == 2
    assert summary["overall_all"]["mean"] == pytest.approx(0.019)
    assert summary["by_group_all"]["corporate"]["n"] == 2
    assert summary["by_source_all"]["rbc"]["n"] == 2


def test_evaluate_filters_credibility_adv_and_missing_abnormal() -> None:
    with pool().connection() as conn:
        backfill = _source(conn, "wayback:rbc", 0.8)
        live = _source(conn, "rbc", 0.8)
        weak = _source(conn, "smartlab", 0.4)
        _instrument(conn, "SBER")
        _train_marker(conn, backfill)
        _article(
            conn,
            live,
            tag="good",
            ticker="SBER",
            published=datetime(2026, 1, 5),
            adv=2_000_000.0,
        )
        _article(
            conn,
            weak,
            tag="weak-source",
            ticker="SBER",
            published=datetime(2026, 1, 6),
            adv=2_000_000.0,
        )
        _article(
            conn,
            live,
            tag="illiquid",
            ticker="SBER",
            published=datetime(2026, 1, 7),
            adv=1_000.0,
        )
        _article(
            conn,
            live,
            tag="no-abnormal",
            ticker="SBER",
            published=datetime(2026, 1, 8),
            abnormal_ret=None,
            ret_1d=0.02,
            adv=2_000_000.0,
        )
        conn.commit()

    common = {
        "horizon": 1,
        "key_mode": "event",
        "min_marker_n": 5,
        "min_t": 0.0,
        "min_edge": 0.0,
    }
    strict = evaluate(
        "2026-01-01",
        "2026-01-01",
        min_credibility=0.5,
        min_adv=500_000.0,
        **common,
    )
    assert strict["pairs"] == 1

    abnormal_only = evaluate(
        "2026-01-01",
        "2026-01-01",
        min_credibility=0.0,
        min_adv=0.0,
        **common,
    )
    assert abnormal_only["pairs"] == 3

    with_fallback = evaluate(
        "2026-01-01",
        "2026-01-01",
        min_credibility=0.0,
        min_adv=0.0,
        require_abnormal=False,
        **common,
    )
    assert with_fallback["pairs"] == 4

    macro_only = evaluate(
        "2026-01-01",
        "2026-01-01",
        only_group="macro",
        min_credibility=0.0,
        min_adv=0.0,
        **common,
    )
    assert macro_only["pairs"] == 0


def test_evaluate_selects_requested_horizon() -> None:
    with pool().connection() as conn:
        backfill = _source(conn, "wayback:rbc", 0.8)
        live = _source(conn, "rbc", 0.8)
        _instrument(conn, "SBER")
        for index in range(10):
            _article(
                conn,
                backfill,
                tag=f"train-{index}",
                ticker="SBER",
                published=datetime(2025, 12, 1),
                abnormal_ret=0.01,
                abnormal_3d=0.02,
                abnormal_5d=-0.01,
            )
        _article(
            conn,
            live,
            tag="test",
            ticker="SBER",
            published=datetime(2026, 1, 5),
            abnormal_ret=0.02,
            abnormal_3d=-0.01,
            abnormal_5d=0.03,
        )
        conn.commit()

    common = {
        "key_mode": "event",
        "min_marker_n": 5,
        "min_t": 0.0,
        "min_edge": 0.0,
    }
    daily = evaluate("2026-01-01", "2026-01-01", horizon=1, **common)
    three = evaluate("2026-01-01", "2026-01-01", horizon=3, **common)
    week = evaluate("2026-01-01", "2026-01-01", horizon=5, **common)

    assert daily["overall_all"]["mean"] == pytest.approx(0.019)
    assert three["overall_all"]["mean"] == pytest.approx(-0.011)
    assert week["overall_all"]["mean"] == pytest.approx(-0.031)


def test_event_study_reports_groups_and_ci() -> None:
    with pool().connection() as conn:
        source = _source(conn, "rbc", 0.8)
        _instrument(conn, "SBER")
        rows = [
            ("corp-pos-1", "dividend", 1.0, 0.02),
            ("corp-pos-2", "dividend", 1.0, 0.02),
            ("corp-pos-3", "dividend", 1.0, 0.02),
            ("corp-neg-1", "dividend", -1.0, -0.01),
            ("corp-neg-2", "dividend", -1.0, -0.01),
            ("macro-pos", "rate", 1.0, 0.005),
            ("macro-neg", "rate", -1.0, -0.02),
        ]
        for tag, event_type, sentiment, value in rows:
            _article(
                conn,
                source,
                tag=tag,
                ticker="SBER",
                published=datetime(2026, 1, 5),
                event_type=event_type,
                sentiment=sentiment,
                abnormal_ret=value,
            )
        conn.commit()

    result = event_study(horizon=1)

    assert result["n"] == 7
    assert result["groups"]["corporate|pos"]["n"] == 3
    assert result["groups"]["corporate|pos"]["mean"] == pytest.approx(0.02)
    assert result["groups"]["corporate|neg"]["mean"] == pytest.approx(-0.01)
    assert result["groups"]["macro|neg"]["mean"] == pytest.approx(-0.02)
    assert "ci_low" in result["groups"]["corporate|pos"]


def test_add_months_handles_month_and_year_edges() -> None:
    assert _add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert _add_months(date(2026, 3, 31), -1) == date(2026, 2, 28)
    assert _add_months(date(2026, 11, 15), 3) == date(2027, 2, 15)
    assert _add_months(date(2026, 12, 31), 1) == date(2027, 1, 31)


def test_walk_forward_pools_sequential_folds() -> None:
    with pool().connection() as conn:
        backfill = _source(conn, "wayback:rbc", 0.8)
        live = _source(conn, "rbc", 0.8)
        _instrument(conn, "SBER")
        _train_marker(conn, backfill)
        for month in (2, 3):
            for index in range(2):
                _article(
                    conn,
                    live,
                    tag=f"fold-{month}-{index}",
                    ticker="SBER",
                    published=datetime(2026, month, 10 + index),
                )
        conn.commit()

    result = walk_forward(
        "2026-02-01",
        "2026-04-01",
        1,
        horizon=1,
        key_mode="event",
        min_marker_n=5,
        min_t=0.0,
        min_edge=0.0,
    )

    assert len(result["folds"]) == 2
    assert result["folds"][0]["pairs"] == 2
    assert result["folds"][1]["pairs"] == 2
    assert result["pairs"] == 4
    assert result["pooled"]["mean"] == pytest.approx(0.019)
    assert result["pooled"]["ci_low"] <= result["pooled"]["mean"]
    assert result["pooled"]["ci_high"] >= result["pooled"]["mean"]


def test_event_study_demean_removes_common_day_drift() -> None:
    with pool().connection() as conn:
        source = _source(conn, "rbc", 0.8)
        _instrument(conn, "SBER")
        for index in range(3):
            _article(
                conn,
                source,
                tag=f"day1-pos-{index}",
                ticker="SBER",
                published=datetime(2026, 1, 5),
                sentiment=1.0,
                abnormal_ret=0.02,
            )
        for index in range(3):
            _article(
                conn,
                source,
                tag=f"day2-neg-{index}",
                ticker="SBER",
                published=datetime(2026, 1, 6),
                sentiment=-1.0,
                abnormal_ret=-0.02,
            )
        conn.commit()

    raw = event_study(horizon=1)
    demeaned = event_study(horizon=1, demean_by_day=True)

    assert raw["spreads"]["corporate"]["spread"] == pytest.approx(0.04)
    assert demeaned["spreads"]["corporate"]["spread"] == pytest.approx(0.0)
    assert demeaned["groups"]["corporate|pos"]["mean"] == pytest.approx(0.0)
    assert demeaned["groups"]["corporate|neg"]["mean"] == pytest.approx(0.0)


def test_portfolio_backtest_compounds_signals() -> None:
    with pool().connection() as conn:
        backfill = _source(conn, "wayback:rbc", 0.8)
        live = _source(conn, "rbc", 0.8)
        _instrument(conn, "SBER")
        values = [0.005, 0.008, 0.010, 0.012, 0.015] * 2
        for index, value in enumerate(values):
            _article(
                conn,
                backfill,
                tag=f"train-{index}",
                ticker="SBER",
                published=datetime(2025, 12, 1),
                abnormal_ret=value,
            )
        for index in range(4):
            _article(
                conn,
                live,
                tag=f"test-{index}",
                ticker="SBER",
                published=datetime(2026, 1, 5 + 2 * index),
                abnormal_ret=0.02,
            )
        conn.commit()

    result = portfolio_backtest(
        "2026-01-01",
        "2026-01-01",
        horizon=1,
        key_mode="event",
        min_marker_n=5,
        min_t=2.0,
        min_edge=0.0,
        per_trade_pct=0.1,
        max_positions=10,
    )

    assert result["trades"] == 4
    assert result["opened"] == 4
    assert result["hit"] == pytest.approx(1.0)
    assert result["max_drawdown"] == pytest.approx(0.0)
    assert result["total_return"] == pytest.approx((1 + 0.1 * 0.019) ** 4 - 1)
