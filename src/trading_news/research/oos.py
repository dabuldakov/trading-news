"""Out-of-sample проверка новостных маркеров.

Маркеры строятся только на обучающем периоде (``t0 < train_end``), затем
оцениваются сигналы на тестовом (``published_at >= test_start``). Это убирает
главную проблему in-sample: сигнал не «видел» реакцию, на которой посчитан.
"""

from __future__ import annotations

import argparse
import logging
import math
from statistics import mean as stat_mean
from statistics import stdev

from ..db import pool

logger = logging.getLogger(__name__)

COST = 0.001
MIN_MARKER_N = 5


def _label(sentiment: float) -> str:
    if sentiment > 0:
        return "pos"
    if sentiment < 0:
        return "neg"
    return "neutral"


def _source_key(source: str | None) -> str:
    text = (source or "unknown").strip()
    return text.split(":", 1)[-1] if ":" in text else text


def _stats(values: list[float]) -> dict:
    if not values:
        return {"n": 0, "mean": 0.0, "t": 0.0, "hit": 0.0}
    avg = stat_mean(values)
    std = stdev(values) if len(values) > 1 else 0.0
    t = avg / (std / math.sqrt(len(values))) if std > 0 else 0.0
    return {
        "n": len(values),
        "mean": avg,
        "t": t,
        "hit": sum(1 for v in values if v > 0) / len(values),
    }


def _train_markers(conn, train_end: str) -> dict[str, dict]:
    rows = conn.execute(
        """
        SELECT n.event_type, n.sentiment, s.name AS source,
               COALESCE(r.abnormal_ret, r.ret_1d) AS reaction
        FROM news_reactions r
        JOIN news_analysis n ON n.article_id = r.article_id
        JOIN news_articles a ON a.id = r.article_id
        LEFT JOIN news_sources s ON s.id = a.source_id
        WHERE r.t0 < %s
          AND COALESCE(r.abnormal_ret, r.ret_1d) IS NOT NULL
        """,
        (train_end,),
    ).fetchall()
    groups: dict[str, list[float]] = {}
    for event_type, sentiment, source, reaction in rows:
        key = f"{event_type}|{_label(sentiment)}|{_source_key(source)}"
        groups.setdefault(key, []).append(float(reaction))
    return {key: _stats(values) for key, values in groups.items()}


def evaluate(
    train_end: str,
    test_start: str,
    *,
    cost: float = COST,
    min_edge: float = 0.001,
) -> dict:
    with pool().connection() as conn:
        markers = _train_markers(conn, train_end)
        rows = conn.execute(
            """
            SELECT a.published_at, t.ticker, n.event_type, n.sentiment,
                   s.name AS source, r.ret_1d, r.abnormal_ret
            FROM news_articles a
            JOIN news_tickers t ON t.article_id = a.id
            JOIN news_analysis n ON n.article_id = a.id
            JOIN news_reactions r
              ON r.article_id = a.id AND r.ticker = t.ticker
            LEFT JOIN news_sources s ON s.id = a.source_id
            WHERE a.published_at >= %s
            """,
            (test_start,),
        ).fetchall()

    results: list[dict] = []
    all_pairs: list[dict] = []
    for _published, ticker, event_type, sentiment, source, ret_1d, abnormal in rows:
        key = f"{event_type}|{_label(sentiment)}|{_source_key(source)}"
        marker = markers.get(key)
        if marker is None or marker["n"] < MIN_MARKER_N:
            continue
        reaction = abnormal if abnormal is not None else ret_1d
        if reaction is None:
            continue
        side = 1 if marker["mean"] > cost else -1
        pnl = side * float(reaction) - cost
        all_pairs.append(
            {
                "marker": key,
                "source": _source_key(source),
                "ticker": ticker,
                "side": side,
                "reaction": float(reaction),
                "pnl": pnl,
            }
        )
        edge = marker["mean"] - cost
        if abs(edge) < min_edge:
            continue
        results.append(
            {
                "marker": key,
                "source": _source_key(source),
                "ticker": ticker,
                "side": side,
                "reaction": float(reaction),
                "pnl": pnl,
            }
        )

    overall = _stats([row["pnl"] for row in results])
    overall_all = _stats([row["pnl"] for row in all_pairs])
    by_marker: dict[str, dict] = {}
    by_source: dict[str, dict] = {}
    by_marker_all: dict[str, dict] = {}
    for row in results:
        by_marker.setdefault(row["marker"], []).append(row["pnl"])
        by_source.setdefault(row["source"], []).append(row["pnl"])
    for row in all_pairs:
        by_marker_all.setdefault(row["marker"], []).append(row["pnl"])

    return {
        "train_end": train_end,
        "test_start": test_start,
        "markers_train": len(markers),
        "signals_oos": len(results),
        "overall": overall,
        "overall_all": overall_all,
        "by_marker": {k: _stats(v) for k, v in by_marker.items()},
        "by_marker_all": {k: _stats(v) for k, v in by_marker_all.items()},
        "by_source": {k: _stats(v) for k, v in by_source.items()},
    }


def _print(summary: dict) -> None:
    print(
        f"OOS: train<{summary['train_end']} test>={summary['test_start']} | "
        f"маркеров {summary['markers_train']} | сигналов {summary['signals_oos']}"
    )
    o = summary["overall"]
    print(
        f"  filtered: n={o['n']} mean={o['mean']*100:+.3f}% "
        f"(t {o['t']:+.1f}) hit {o['hit']:.0%}"
    )
    a = summary["overall_all"]
    print(
        f"  ALL pairs: n={a['n']} mean={a['mean']*100:+.3f}% "
        f"(t {a['t']:+.1f}) hit {a['hit']:.0%}"
    )
    for key, st in sorted(
        summary["by_marker_all"].items(), key=lambda kv: kv[1]["mean"], reverse=True
    )[:12]:
        print(
            f"  {key:<28} n={st['n']:<4} {st['mean']*100:+.3f}% "
            f"(t {st['t']:+.1f}) hit {st['hit']:.0%}"
        )
    for key, st in summary["by_source"].items():
        print(
            f"  source {key:<16} n={st['n']:<4} {st['mean']*100:+.3f}% "
            f"(t {st['t']:+.1f})"
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="OOS news markers")
    parser.add_argument("--train-end", default="2026-07-01")
    parser.add_argument("--test-start", default="2026-07-01")
    parser.add_argument("--cost", type=float, default=COST)
    args = parser.parse_args()
    _print(
        evaluate(
            args.train_end,
            args.test_start,
            cost=args.cost,
        )
    )
