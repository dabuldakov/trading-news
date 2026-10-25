"""Sector flow: новостной поток по секторам → доходность секторных корзин.

Идея: sentiment отдельной бумаги не даёт эджа, но агрегированный поток новостей
по сектору может предсказывать относительную доходность сектора. Строим
equal-weight корзины из ликвидных тикеров, агрегируем новости по (день, сектор)
и проверяем rank IC / long-short / event-time спреды.
"""

from __future__ import annotations

import argparse
import logging
import math
from collections import defaultdict
from datetime import date, timedelta
from statistics import mean as stat_mean
from statistics import stdev

from ..db import pool
from .event_study import _candles, _market
from .stats import permutation_spread, stats

logger = logging.getLogger(__name__)

MARKET = "__market__"
COST = 0.001


def _active_tickers() -> dict[str, list[str]]:
    with pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT c.sector, i.ticker
            FROM instruments i
            JOIN companies c ON c.id = i.company_id
            WHERE c.sector IS NOT NULL
              AND c.sector <> ''
              AND i.status = 'active'
            ORDER BY c.sector, i.ticker
            """
        ).fetchall()
    grouped: dict[str, list[str]] = defaultdict(list)
    for sector, ticker in rows:
        grouped[sector].append(ticker)
    return dict(grouped)


def _closes(ticker: str, start: str, end: str) -> dict[date, float]:
    bars = _candles(ticker, "TQBR", start, end)
    return {bar["begin"].date(): bar["close"] for bar in bars}


def _daily_returns(series: dict[str, dict[date, float]]) -> dict[date, float]:
    dates = sorted({day for closes in series.values() for day in closes})
    out: dict[date, float] = {}
    for previous, current in zip(dates, dates[1:], strict=False):
        values = []
        for closes in series.values():
            before = closes.get(previous)
            after = closes.get(current)
            if before and after and before > 0:
                values.append(after / before - 1.0)
        if values:
            out[current] = stat_mean(values)
    return out


def build_sector_returns(start: str, end: str) -> int:
    """Построить дневные equal-weight доходности секторов и рынка (IMOEX)."""
    tickers_by_sector = _active_tickers()
    fetch_start = (date.fromisoformat(start) - timedelta(days=10)).isoformat()
    merged: dict[str, list[str]] = {
        sector: tickers for sector, tickers in tickers_by_sector.items()
    }
    all_tickers = sorted(
        {ticker for tickers in tickers_by_sector.values() for ticker in tickers}
    )
    closes_by_ticker: dict[str, dict[date, float]] = {}
    for ticker in all_tickers:
        try:
            closes_by_ticker[ticker] = _closes(ticker, fetch_start, end)
        except Exception as exc:  # noqa: BLE001 - one ticker must not break
            logger.warning("sector candles %s: %s", ticker, exc)
    try:
        market_closes = {
            bar["begin"].date(): bar["close"]
            for bar in _market(fetch_start, end)
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("sector market: %s", exc)
        market_closes = {}

    first = date.fromisoformat(start)
    records: list[tuple] = []
    for sector, tickers in merged.items():
        series = {
            ticker: closes_by_ticker[ticker]
            for ticker in tickers
            if ticker in closes_by_ticker and closes_by_ticker[ticker]
        }
        if not series:
            continue
        returns = _daily_returns(series)
        for day, value in returns.items():
            if day >= first:
                records.append(
                    (
                        day.isoformat(),
                        sector,
                        value,
                        sum(1 for ticker in series if day in series[ticker]),
                    )
                )
    market_returns = _daily_returns({MARKET: market_closes})
    for day, value in market_returns.items():
        if day >= first:
            records.append((day.isoformat(), MARKET, value, 1))
    if not records:
        return 0

    with pool().connection() as conn:
        conn.execute(
            "DELETE FROM sector_returns WHERE day BETWEEN %s AND %s",
            (start, end),
        )
        with conn.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO sector_returns (day, sector, ret_1d, n_tickers)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (day, sector) DO UPDATE SET
                    ret_1d = EXCLUDED.ret_1d,
                    n_tickers = EXCLUDED.n_tickers
                """,
                records,
            )
        conn.commit()
    return len(records)


def _load_returns(start: str, end: str) -> dict[tuple[date, str], float]:
    with pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT day, sector, ret_1d
            FROM sector_returns
            WHERE day BETWEEN %s AND %s AND ret_1d IS NOT NULL
            """,
            (start, end),
        ).fetchall()
    return {(day, sector): float(value) for day, sector, value in rows}


def _load_flow(start: str, end: str) -> dict[tuple[date, str], dict]:
    with pool().connection() as conn:
        rows = conn.execute(
            """
            WITH x AS (
                SELECT DISTINCT a.id, a.published_at::date AS day,
                       c.sector, n.sentiment
                FROM news_articles a
                JOIN news_tickers t ON t.article_id = a.id
                JOIN instruments i ON i.ticker = t.ticker
                JOIN companies c ON c.id = i.company_id
                JOIN news_analysis n ON n.article_id = a.id
                WHERE c.sector IS NOT NULL AND c.sector <> ''
                  AND a.published_at >= %s
                  AND a.published_at < (%s::date + interval '1 day')
            )
            SELECT day, sector, count(*) AS n,
                   avg(sentiment) AS sentiment,
                   avg(CASE WHEN sentiment > 0 THEN 1.0
                            WHEN sentiment < 0 THEN -1.0 ELSE 0.0 END) AS net
            FROM x
            GROUP BY day, sector
            """,
            (start, end),
        ).fetchall()
    return {
        (day, sector): {
            "n": int(count),
            "sentiment": float(sentiment or 0.0),
            "net": float(net or 0.0),
        }
        for day, sector, count, sentiment, net in rows
    }


def forward_records(
    returns: dict[tuple[date, str], float],
    flow: dict[tuple[date, str], dict],
    horizon: int = 1,
) -> list[dict]:
    days = sorted({day for (day, sector) in returns if sector != MARKET})
    index = {day: position for position, day in enumerate(days)}
    market = {
        day: returns.get((day, MARKET))
        for day in days
    }
    records = []
    for (day, sector), values in flow.items():
        position = index.get(day)
        if position is None or position + horizon >= len(days):
            continue
        forward = 1.0
        benchmark = 1.0
        valid = True
        for offset in range(1, horizon + 1):
            value = returns.get((days[position + offset], sector))
            bench = market.get(days[position + offset])
            if value is None or bench is None:
                valid = False
                break
            forward *= 1.0 + value
            benchmark *= 1.0 + bench
        if not valid:
            continue
        records.append(
            {
                "day": day,
                "sector": sector,
                "sentiment": values["sentiment"],
                "net": values["net"],
                "n": values["n"],
                "ret": forward - 1.0,
                "excess": (forward - 1.0) - (benchmark - 1.0),
            }
        )
    return records


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    index = 0
    while index < len(values):
        end = index
        while end + 1 < len(values) and (
            values[order[end + 1]] == values[order[index]]
        ):
            end += 1
        rank = (index + end) / 2.0 + 1.0
        for position in range(index, end + 1):
            ranks[order[position]] = rank
        index = end + 1
    return ranks


def spearman(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or len(left) < 3:
        return 0.0
    ranks_left = _ranks(left)
    ranks_right = _ranks(right)
    mean_left = stat_mean(ranks_left)
    mean_right = stat_mean(ranks_right)
    numerator = sum(
        (a - mean_left) * (b - mean_right)
        for a, b in zip(ranks_left, ranks_right, strict=True)
    )
    denom_left = math.sqrt(sum((a - mean_left) ** 2 for a in ranks_left))
    denom_right = math.sqrt(sum((b - mean_right) ** 2 for b in ranks_right))
    if denom_left == 0 or denom_right == 0:
        return 0.0
    return numerator / (denom_left * denom_right)


def analyze(records: list[dict], *, top: int = 3, cost: float = COST) -> dict:
    buckets: dict[str, list[float]] = {"pos": [], "neg": [], "neutral": []}
    for row in records:
        if row["sentiment"] > 0:
            buckets["pos"].append(row["excess"])
        elif row["sentiment"] < 0:
            buckets["neg"].append(row["excess"])
        else:
            buckets["neutral"].append(row["excess"])
    bucket_stats = {key: stats(values) for key, values in buckets.items()}
    spread, pvalue = permutation_spread(buckets["pos"], buckets["neg"])
    bucket_stats["spread"] = {
        "mean": spread,
        "p": pvalue,
        "n": len(buckets["pos"]) + len(buckets["neg"]),
    }

    by_day: dict[date, list[dict]] = defaultdict(list)
    for row in records:
        by_day[row["day"]].append(row)

    ic_sentiment = []
    ic_intensity = []
    long_short = []
    for day_rows in by_day.values():
        if len(day_rows) < 3:
            continue
        ic_sentiment.append(
            spearman(
                [row["sentiment"] for row in day_rows],
                [row["excess"] for row in day_rows],
            )
        )
        ic_intensity.append(
            spearman(
                [math.log1p(row["n"]) for row in day_rows],
                [row["excess"] for row in day_rows],
            )
        )
        if len(day_rows) >= max(4, 2 * top):
            ordered = sorted(day_rows, key=lambda row: row["sentiment"])
            longs = [row["excess"] for row in ordered[-top:]]
            shorts = [row["excess"] for row in ordered[:top]]
            long_short.append(stat_mean(longs) - stat_mean(shorts) - 2 * cost)

    def _ic(values: list[float]) -> dict:
        item = stats(values)
        item["ir"] = (
            item["mean"] / stdev(values)
            if len(values) > 1 and stdev(values) > 0
            else 0.0
        )
        return item

    return {
        "observations": len(records),
        "days": len(by_day),
        "buckets": bucket_stats,
        "ic_sentiment": _ic(ic_sentiment),
        "ic_intensity": _ic(ic_intensity),
        "long_short": stats(long_short),
    }


def _print(result: dict, *, start: str, end: str, horizon: int) -> None:
    print(
        f"SECTOR FLOW h={horizon}d window={start}..{end} "
        f"obs={result['observations']} days={result['days']}"
    )
    print("  -- event buckets (excess vs IMOEX) --")
    for key in ("pos", "neg", "neutral"):
        item = result["buckets"][key]
        print(
            f"  {key:<8} n={item['n']:<5} {item['mean'] * 100:+.3f}% "
            f"(t {item['t']:+.1f}) hit {item['hit']:.0%}"
        )
    spread = result["buckets"]["spread"]
    print(
        f"  spread   {spread['mean'] * 100:+.3f}% p={spread['p']:.3f} "
        f"(n={spread['n']})"
    )
    print("  -- cross-sectional --")
    for name in ("ic_sentiment", "ic_intensity"):
        item = result[name]
        print(
            f"  {name:<14} mean={item['mean']:+.3f} t={item['t']:+.1f} "
            f"IR={item['ir']:+.2f} n_days={item['n']}"
        )
    ls = result["long_short"]
    print(
        f"  long-short top   n={ls['n']:<4} {ls['mean'] * 100:+.3f}% "
        f"(t {ls['t']:+.1f}) hit {ls['hit']:.0%}"
    )


def run(
    start: str,
    end: str,
    *,
    horizon: int = 1,
    top: int = 3,
    build: bool = True,
) -> dict:
    if build:
        rows = build_sector_returns(start, end)
        logger.info("sector returns: %s rows", rows)
    returns = _load_returns(start, end)
    flow = _load_flow(start, end)
    records = forward_records(returns, flow, horizon)
    return analyze(records, top=top)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="Sector news-flow research")
    parser.add_argument("--start", default="2024-10-01")
    parser.add_argument("--end", default="2026-10-20")
    parser.add_argument("--horizon", type=int, default=1)
    parser.add_argument("--top", type=int, default=3)
    parser.add_argument("--skip-build", action="store_true")
    args = parser.parse_args()
    result = run(
        args.start,
        args.end,
        horizon=args.horizon,
        top=args.top,
        build=not args.skip_build,
    )
    _print(
        result,
        start=args.start,
        end=args.end,
        horizon=args.horizon,
    )
