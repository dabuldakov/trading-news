"""Форма реакции на новости: сильные/слабые знаки, интрадей и пост-дрифт.

Считает:
* abnormal 1/3/5д по бакетам sentiment (strong/weak pos/neg);
* t0 open→close (интрадей) из gap и ret_1d;
* пост-дрифт от close(t0) к +2/+4 торговым дням;
* raw и day-demeaned (убираем общий рыночный дрейф).
"""

from __future__ import annotations

import argparse
import logging
from collections import defaultdict
from statistics import mean

from ..db import pool
from .stats import stats

logger = logging.getLogger(__name__)

BUCKETS = ("strong_neg", "weak_neg", "neutral", "weak_pos", "strong_pos")
GAP_BUCKETS = ("gap_down_big", "gap_down", "gap_up", "gap_up_big")


def bucket(sentiment: float, score: float) -> str:
    if sentiment < 0:
        return "strong_neg" if score <= -0.5 else "weak_neg"
    if sentiment > 0:
        return "strong_pos" if score >= 0.5 else "weak_pos"
    return "neutral"


def gap_bucket(gap: float | None) -> str | None:
    if gap is None:
        return None
    if gap <= -0.005:
        return "gap_down_big"
    if gap < 0:
        return "gap_down"
    if gap < 0.005:
        return "gap_up"
    return "gap_up_big"


def derive(gap: float | None, ret_1d, ret_3d, ret_5d) -> dict:
    result = {"intraday": None, "post2": None, "post4": None}
    if gap is None or ret_1d is None or gap <= -0.99 or ret_1d <= -0.99:
        return result
    result["intraday"] = (1.0 + ret_1d) / (1.0 + gap) - 1.0
    if ret_3d is not None and ret_3d > -0.99:
        result["post2"] = (1.0 + ret_3d) / (1.0 + ret_1d) - 1.0
    if ret_5d is not None and ret_5d > -0.99:
        result["post4"] = (1.0 + ret_5d) / (1.0 + ret_1d) - 1.0
    return result


def load_records(days: int = 900) -> list[dict]:
    with pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT a.published_at::date AS day, n.sentiment, n.score,
                   r.gap, r.ret_1d, r.ret_3d, r.ret_5d,
                   r.abnormal_ret, r.abnormal_3d, r.abnormal_5d
            FROM news_reactions r
            JOIN news_analysis n ON n.article_id = r.article_id
            JOIN news_articles a ON a.id = r.article_id
            WHERE a.published_at >= now() - (%s || ' days')::interval
            """,
            (days,),
        ).fetchall()
    return [
        {
            "day": row[0],
            "sentiment": float(row[1] or 0.0),
            "score": float(row[2] or 0.0),
            "gap": float(row[3]) if row[3] is not None else None,
            "ret_1d": float(row[4]) if row[4] is not None else None,
            "ret_3d": float(row[5]) if row[5] is not None else None,
            "ret_5d": float(row[6]) if row[6] is not None else None,
            "abn1": float(row[7]) if row[7] is not None else None,
            "abn3": float(row[8]) if row[8] is not None else None,
            "abn5": float(row[9]) if row[9] is not None else None,
        }
        for row in rows
    ]


def _by_day(records: list[dict], field: str) -> dict:
    values: dict = defaultdict(list)
    for record in records:
        if record.get(field) is not None:
            values[record["day"]].append(record[field])
    return {day: mean(items) for day, items in values.items()}


def _bucket_stats(records: list[dict], field: str, *, demean: bool) -> dict[str, dict]:
    day_mean = _by_day(records, field) if demean else {}
    groups: dict[str, list[float]] = defaultdict(list)
    for record in records:
        value = record.get(field)
        if value is None:
            continue
        base = day_mean.get(record["day"], 0.0) if demean else 0.0
        groups[bucket(record["sentiment"], record["score"])].append(value - base)
    return {key: stats(groups.get(key, [])) for key in BUCKETS}


def analyze(records: list[dict]) -> dict:
    for record in records:
        record.update(
            derive(
                record["gap"],
                record["ret_1d"],
                record["ret_3d"],
                record["ret_5d"],
            )
        )
    result = {
        "n": len(records),
        "h1": {
            "raw": _bucket_stats(records, "abn1", demean=False),
            "demeaned": _bucket_stats(records, "abn1", demean=True),
        },
        "h3": {
            "raw": _bucket_stats(records, "abn3", demean=False),
            "demeaned": _bucket_stats(records, "abn3", demean=True),
        },
        "h5": {
            "raw": _bucket_stats(records, "abn5", demean=False),
            "demeaned": _bucket_stats(records, "abn5", demean=True),
        },
        "intraday": _bucket_stats(records, "intraday", demean=True),
        "post2": {
            "raw": _bucket_stats(records, "post2", demean=False),
            "demeaned": _bucket_stats(records, "post2", demean=True),
        },
        "post4": {
            "raw": _bucket_stats(records, "post4", demean=False),
            "demeaned": _bucket_stats(records, "post4", demean=True),
        },
    }
    gaps: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for record in records:
        value = record.get("post2")
        gb = gap_bucket(record.get("gap"))
        if value is None or gb is None:
            continue
        key = bucket(record["sentiment"], record["score"])
        gaps[key][gb].append(value)
    result["gap_post2"] = {
        key: {gb: stats(values) for gb, values in buckets.items()}
        for key, buckets in gaps.items()
    }
    return result


def _print(result: dict) -> None:
    print(f"NEWS SHAPE n={result['n']}")
    for horizon in ("h1", "h3", "h5"):
        print(f"== {horizon} ==")
        for name in ("raw", "demeaned"):
            print(f"  {name}")
            for key in BUCKETS:
                item = result[horizon][name][key]
                print(
                    f"    {key:<10} n={item['n']:<5} "
                    f"{item['mean'] * 100:+.3f}% (t {item['t']:+.1f}) "
                    f"hit {item['hit']:.0%}"
                )
    print("== intraday t0 (demeaned) ==")
    for key in BUCKETS:
        item = result["intraday"][key]
        print(
            f"  {key:<10} n={item['n']:<5} {item['mean'] * 100:+.3f}% "
            f"(t {item['t']:+.1f})"
        )
    for name in ("post2", "post4"):
        print(f"== {name} ==")
        for variant in ("raw", "demeaned"):
            print(f"  {variant}")
            for key in BUCKETS:
                item = result[name][variant][key]
                print(
                    f"    {key:<10} n={item['n']:<5} "
                    f"{item['mean'] * 100:+.3f}% (t {item['t']:+.1f})"
                )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="News reaction shape")
    parser.add_argument("--days", type=int, default=900)
    args = parser.parse_args()
    _print(analyze(load_records(args.days)))
