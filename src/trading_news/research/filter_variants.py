"""Compare news signal quality gates and their OOS sample sizes."""

from __future__ import annotations

import json
import math
import os
from collections import defaultdict

import psycopg

HORIZONS = {"1d": "abnormal_ret", "3d": "abnormal_3d", "5d": "abnormal_5d"}
TICKERS = [5, 10, 20, 999]
LINKS = [0.80, 0.90, 0.95]
PERIODS = ["train", "test"]


def stats(values):
    n = len(values)
    if n < 2:
        return {"n": n, "mean_bp": None, "t": None, "hit": None}
    mean = sum(values) / n
    sd = math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1))
    return {
        "n": n,
        "mean_bp": mean * 10000,
        "t": mean / (sd / math.sqrt(n)) if sd else None,
        "hit": sum(1 for v in values if v > 0) / n,
    }


def main():
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        rows = conn.execute("""
            SELECT a.id, a.published_at::date, n.event_type, n.sentiment,
                   t.confidence,
                   (SELECT count(*) FROM news_tickers t2 WHERE t2.article_id=a.id),
                   r.abnormal_ret, r.abnormal_3d, r.abnormal_5d
            FROM news_articles a
            JOIN news_analysis n ON n.article_id=a.id
            JOIN news_tickers t ON t.article_id=a.id
            JOIN news_reactions r ON r.article_id=a.id AND r.ticker=t.ticker
            WHERE a.published_at >= '2024-01-01'
        """).fetchall()

    out = {
        "variants": [],
        "filters": {"event_type": "!= other", "sentiment_zero": "skip"},
    }
    for max_t in TICKERS:
        for min_link in LINKS:
            selected = []
            for row in rows:
                article_id, day, event_type, sentiment, conf, tcount, *rets = row
                if event_type == "other" or int(tcount or 0) > max_t:
                    continue
                if float(conf or 0) < min_link:
                    continue
                direction = (
                    1 if (sentiment or 0) > 0 else -1 if (sentiment or 0) < 0 else 0
                )
                if not direction:
                    continue
                selected.append((article_id, day, event_type, direction, rets))
            articles = {r[0] for r in selected}
            variant = {
                "max_tickers": max_t,
                "min_link": min_link,
                "articles": len(articles),
                "rows": len(selected),
                "by_event": {},
                "all": {},
            }
            for label, _idx in HORIZONS.items():
                for period in PERIODS:
                    vals = []
                    by_et = defaultdict(list)
                    for _aid, day, et, direction, rets in selected:
                        if (period == "train") != (day.year < 2026):
                            continue
                        value = rets[list(HORIZONS).index(label)]
                        if value is None:
                            continue
                        vals.append(float(value) * direction)
                        by_et[et].append(float(value) * direction)
                    variant["all"][f"{label}_{period}"] = stats(vals)
                    for et, evals in by_et.items():
                        if len(evals) >= 30:
                            variant["by_event"].setdefault(et, {})[
                                f"{label}_{period}"
                            ] = stats(evals)
            out["variants"].append(variant)
    # Print compact comparison.
    print("max_t link articles rows | test_1d n mean_bp t | test_5d n mean_bp t")
    for v in out["variants"]:
        a = v["all"]["1d_test"]
        b = v["all"]["5d_test"]
        print(
            f"{v['max_tickers']:5d} {v['min_link']:.2f} {v['articles']:8d} {v['rows']:4d} | "
            f"{a['n']:4d} {a['mean_bp'] or 0:+7.2f} {a['t'] or 0:+5.2f} | "
            f"{b['n']:4d} {b['mean_bp'] or 0:+7.2f} {b['t'] or 0:+5.2f}"
        )
    with open("/tmp/filter_variants.json", "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    main()
