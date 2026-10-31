from __future__ import annotations

from datetime import date

import pytest

from trading_news.research.news_shape import analyze, bucket, derive, gap_bucket


def test_bucket_boundaries() -> None:
    assert bucket(-1, -1.0) == "strong_neg"
    assert bucket(-1, -0.2) == "weak_neg"
    assert bucket(0, 0.0) == "neutral"
    assert bucket(1, 0.2) == "weak_pos"
    assert bucket(1, 1.0) == "strong_pos"


def test_gap_bucket() -> None:
    assert gap_bucket(None) is None
    assert gap_bucket(-0.02) == "gap_down_big"
    assert gap_bucket(-0.001) == "gap_down"
    assert gap_bucket(0.001) == "gap_up"
    assert gap_bucket(0.02) == "gap_up_big"


def test_derive() -> None:
    result = derive(0.01, 0.05, 0.10, 0.20)
    assert result["intraday"] == pytest.approx((1.05 / 1.01) - 1)
    assert result["post2"] == pytest.approx((1.10 / 1.05) - 1)
    assert result["post4"] == pytest.approx((1.20 / 1.05) - 1)
    assert derive(None, 0.1, 0.1, 0.1)["post2"] is None


def test_analyze_demeans_by_day() -> None:
    day1, day2 = date(2026, 1, 5), date(2026, 1, 6)
    records = [
        {
            "day": day1,
            "sentiment": 1,
            "score": 1.0,
            "gap": 0.0,
            "ret_1d": 0.02,
            "ret_3d": 0.03,
            "ret_5d": 0.04,
            "abn1": 0.02,
            "abn3": 0.03,
            "abn5": 0.04,
        },
        {
            "day": day1,
            "sentiment": -1,
            "score": -1.0,
            "gap": 0.0,
            "ret_1d": 0.00,
            "ret_3d": 0.01,
            "ret_5d": 0.02,
            "abn1": 0.00,
            "abn3": 0.01,
            "abn5": 0.02,
        },
        {
            "day": day2,
            "sentiment": 1,
            "score": 1.0,
            "gap": 0.0,
            "ret_1d": 0.04,
            "ret_3d": 0.05,
            "ret_5d": 0.06,
            "abn1": 0.04,
            "abn3": 0.05,
            "abn5": 0.06,
        },
        {
            "day": day2,
            "sentiment": -1,
            "score": -1.0,
            "gap": 0.0,
            "ret_1d": 0.02,
            "ret_3d": 0.03,
            "ret_5d": 0.04,
            "abn1": 0.02,
            "abn3": 0.03,
            "abn5": 0.04,
        },
    ]
    result = analyze(records)
    assert result["h1"]["raw"]["strong_pos"]["mean"] == pytest.approx(0.03)
    assert result["h1"]["raw"]["strong_neg"]["mean"] == pytest.approx(0.01)
    assert result["h1"]["demeaned"]["strong_pos"]["mean"] == pytest.approx(0.01)
    assert result["h1"]["demeaned"]["strong_neg"]["mean"] == pytest.approx(-0.01)
    assert result["n"] == 4
