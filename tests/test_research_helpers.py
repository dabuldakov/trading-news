from __future__ import annotations

from datetime import date

from trading_news.research.clean_signals import _bh, _stats
from trading_news.research.filter_variants import stats as variant_stats
from trading_news.research.news_shape import analyze, bucket, derive, gap_bucket
from trading_news.research.sector_flow import _daily_returns
from trading_news.research.sector_paper import _forward, _trading_days


def test_signal_stats_and_bh() -> None:
    assert _stats([0.1]) == {"n": 1, "mean_bp": None, "t": None, "hit": None}
    stat = _stats([0.1, 0.2, 0.3])
    assert stat["n"] == 3
    assert stat["mean_bp"] > 0
    q = _bh([(("x",), 0.01), (("y",), 0.04)])
    assert q[("x",)] <= q[("y",)]
    assert variant_stats([0.1, 0.1, 0.1])["n"] == 3


def test_news_shape_helpers() -> None:
    assert bucket(-1, -0.6) == "strong_neg"
    assert bucket(1, 0.1) == "weak_pos"
    assert bucket(0, 0) == "neutral"
    assert gap_bucket(-0.01) == "gap_down_big"
    assert gap_bucket(-0.001) == "gap_down"
    assert gap_bucket(0.001) == "gap_up"
    assert gap_bucket(0.01) == "gap_up_big"
    assert derive(None, 0.1, None, None)["intraday"] is None
    result = analyze(
        [
            {
                "day": date(2026, 1, 1),
                "sentiment": 1,
                "score": 0.8,
                "gap": 0.0,
                "ret_1d": 0.01,
                "ret_3d": 0.02,
                "ret_5d": 0.03,
                "abn1": 0.01,
                "abn3": 0.02,
                "abn5": 0.03,
            }
        ]
    )
    assert result["n"] == 1
    assert "h1" in result


def test_sector_helpers() -> None:
    closes = {
        "A": {date(2026, 1, 1): 100.0, date(2026, 1, 2): 101.0},
        "B": {date(2026, 1, 1): 50.0, date(2026, 1, 2): 49.5},
    }
    returns = _daily_returns(closes)
    assert returns[date(2026, 1, 2)] == 0.0
    series = {
        (date(2026, 1, 1), "tech"): 0.1,
        (date(2026, 1, 2), "tech"): 0.2,
        (date(2026, 1, 2), "__market__"): 0.1,
    }
    days = _trading_days(series)
    assert days == [date(2026, 1, 1), date(2026, 1, 2)]
    forward = _forward(series, days, date(2026, 1, 1), "tech", 1)
    assert forward is not None
    assert forward["excess"] > 0
