"""Clean news event study: event_type + ticker relevance quality gates.

Runs against the news database and compares:
- all linked reactions;
- clean subset: event_type != other, <=5 tickers, link confidence >=0.9.

Returns are market-adjusted; sign follows rules-v2 sentiment.  Statistics are
computed separately for 2024-2025 (train) and 2026 (OOS).
"""

from __future__ import annotations

import json
import math
import os
from collections import defaultdict

import psycopg

HORIZONS = {
    "1d": "abnormal_ret",
    "3d": "abnormal_3d",
    "5d": "abnormal_5d",
}
CLEAN_MAX_TICKERS = 5
CLEAN_MIN_LINK = 0.9


def _stats(values: list[float]) -> dict:
    n = len(values)
    if n < 2:
        return {"n": n, "mean_bp": None, "t": None, "hit": None}
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    sd = math.sqrt(var)
    return {
        "n": n,
        "mean_bp": mean * 10000,
        "t": mean / (sd / math.sqrt(n)) if sd else None,
        "hit": sum(1 for v in values if v > 0) / n,
    }


def _bh(pvals: list[tuple[tuple, float]]) -> dict[tuple, float]:
    out = {}
    ok = [(k, p) for k, p in pvals if p is not None]
    ok.sort(key=lambda x: x[1])
    m = len(ok)
    running = 1.0
    for rank in range(m, 0, -1):
        key, p = ok[rank - 1]
        running = min(running, p * m / rank)
        out[key] = min(1.0, running)
    return out


def main() -> None:
    dsn = os.environ["DATABASE_URL"]
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            """
            SELECT a.id, a.published_at, n.event_type, n.sentiment,
                   s.name AS source, t.ticker, t.confidence,
                   (SELECT count(*) FROM news_tickers t2
                     WHERE t2.article_id = a.id) AS ticker_count,
                   r.abnormal_ret, r.abnormal_3d, r.abnormal_5d
            FROM news_articles a
            JOIN news_analysis n ON n.article_id = a.id
            JOIN news_tickers t ON t.article_id = a.id
            JOIN news_sources s ON s.id = a.source_id AND s.enabled
            JOIN news_reactions r
              ON r.article_id = a.id AND r.ticker = t.ticker
            WHERE a.published_at >= '2024-01-01'
            """
        ).fetchall()

    groups: dict[tuple, list[float]] = defaultdict(list)
    counts = {"all_rows": 0, "clean_rows": 0, "clean_articles": set(),
              "all_articles": set()}
    for (
        article_id,
        published_at,
        event_type,
        sentiment,
        source,
        ticker,
        confidence,
        ticker_count,
        *returns,
    ) in rows:
        direction = 1 if (sentiment or 0) > 0 else -1 if (sentiment or 0) < 0 else 0
        if not direction:
            continue
        counts["all_rows"] += 1
        counts["all_articles"].add(article_id)
        clean = (
            event_type != "other"
            and int(ticker_count or 0) <= CLEAN_MAX_TICKERS
            and float(confidence or 0) >= CLEAN_MIN_LINK
        )
        if clean:
            counts["clean_rows"] += 1
            counts["clean_articles"].add(article_id)
        period = "train" if published_at.year < 2026 else "test"
        mode = "clean" if clean else "all"
        for label, field in HORIZONS.items():
            idx = list(HORIZONS).index(label)
            value = returns[idx]
            if value is None:
                continue
            signed = float(value) * direction
            for et in (event_type, "ALL"):
                groups[(mode, et, label, period)].append(signed)

    estimates = []
    pvals = []
    for key, values in groups.items():
        stat = _stats(values)
        if stat["n"] < 30:
            continue
        est = {"mode": key[0], "event_type": key[1], "horizon": key[2],
               "period": key[3], **stat}
        if stat["t"] is not None:
            p = math.erfc(abs(stat["t"]) / math.sqrt(2))
            pvals.append((key, p))
        estimates.append(est)
    qmap = _bh(pvals)
    for est in estimates:
        est["q"] = qmap.get((est["mode"], est["event_type"], est["horizon"], est["period"]))
    estimates.sort(key=lambda x: (x["mode"], x["period"], x["event_type"], x["horizon"]))
    result = {
        "filters": {"max_tickers": CLEAN_MAX_TICKERS, "min_link_confidence": CLEAN_MIN_LINK,
                    "exclude_event_type": "other", "sentiment_zero": "skipped"},
        "counts": {**{k: v for k, v in counts.items() if not isinstance(v, set)},
                   "all_articles": len(counts["all_articles"]),
                   "clean_articles": len(counts["clean_articles"])},
        "estimates": estimates,
    }
    with open("/tmp/clean_signals_report.json", "w") as handle:
        json.dump(result, handle, indent=2, ensure_ascii=False)
    print(json.dumps(result["counts"], indent=2, ensure_ascii=False))
    for row in estimates:
        if row["mode"] == "clean":
            print(
                f"{row['period']:5s} {row['event_type']:12s} {row['horizon']:2s} "
                f"n={row['n']:4d} mean={row['mean_bp']:+.2f}bp t={row['t'] or 0:+.2f} "
                f"hit={row['hit']:.1%} q={row['q'] or 1:.3f}"
            )


if __name__ == "__main__":
    main()
