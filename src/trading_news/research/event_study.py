"""Event study: news → price reaction → markers → signals.

Daily candles from MOEX ISS (public). For each (article, ticker) with a linked
analysis we find the first trading day after publication and compute the
reaction vs the previous close (and vs IMOEX), MFE/MAE over the horizon, and
volume z-score. Aggregated by (event_type, sentiment, source, sector, regime)
into ``event_markers``; then ``signals`` are built from the marker edge.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from statistics import mean as stat_mean
from statistics import median as stat_median
from statistics import pstdev, stdev
from zoneinfo import ZoneInfo

import httpx

from ..config import settings
from ..db import pool

logger = logging.getLogger(__name__)

HORIZONS = (1, 3, 5)
COST = 0.001  # 10 bp round trip (commission + spread estimate)
MIN_MARKER_N = 5


def _candles(secid: str, board: str, start: str, end: str) -> list[dict]:
    url = (
        f"{settings.moex_iss_base.rstrip('/')}/engines/stock/markets/shares/"
        f"boards/{board}/securities/{secid}/candles.json"
    )
    params = {
        "from": start,
        "till": end,
        "interval": 24,
        "iss.meta": "off",
    }
    with httpx.Client(timeout=settings.http_timeout) as client:
        response = client.get(url, params=params)
        response.raise_for_status()
        payload = response.json()
    block = payload.get("candles") or {}
    columns = block.get("columns") or []
    index = {name: i for i, name in enumerate(columns)}
    out = []
    for row in block.get("data") or []:
        out.append(
            {
                "begin": datetime.fromisoformat(str(row[index["begin"]])),
                "open": float(row[index["open"]]),
                "high": float(row[index["high"]]),
                "low": float(row[index["low"]]),
                "close": float(row[index["close"]]),
                "volume": float(row[index["volume"]]),
            }
        )
    return out


def _market(start: str, end: str) -> list[dict]:
    return _candles("IMOEX", "TQBR", start, end)


def _reaction(bars: list[dict], market: list[dict], published_at: datetime):
    if published_at.tzinfo is not None:
        published_at = published_at.astimezone(ZoneInfo("Europe/Moscow")).replace(
            tzinfo=None
        )
    idx = next((i for i, bar in enumerate(bars) if bar["begin"] >= published_at), None)
    if idx is None or idx < 1:
        return None
    base = bars[idx - 1]["close"]
    if base <= 0:
        return None
    end = min(idx + max(HORIZONS) - 1, len(bars) - 1)
    if end < idx:
        return None

    def ret(horizon: int) -> float | None:
        pos = idx + horizon - 1
        if pos >= len(bars):
            return None
        return bars[pos]["close"] / base - 1.0

    mbase = next(
        (i for i, bar in enumerate(market) if bar["begin"] >= published_at), None
    )
    market_ret = None
    if mbase is not None and mbase >= 1 and mbase < len(market):
        market_ret = market[mbase]["close"] / market[mbase - 1]["close"] - 1.0

    window = bars[idx : end + 1]
    highs = [bar["high"] for bar in window]
    lows = [bar["low"] for bar in window]
    volumes = [bar["volume"] for bar in bars[max(0, idx - 20) : idx]]
    vol_z = 0.0
    if len(volumes) >= 5 and pstdev(volumes) > 0:
        vol_z = float((bars[idx]["volume"] - stat_mean(volumes)) / pstdev(volumes))

    return {
        "t0": bars[idx]["begin"],
        "ret_1d": ret(1),
        "ret_3d": ret(3),
        "ret_5d": ret(5),
        "abnormal_ret": (
            (ret(1) - market_ret)
            if ret(1) is not None and market_ret is not None
            else None
        ),
        "mfe": max(highs) / base - 1.0,
        "mae": min(lows) / base - 1.0,
        "gap": bars[idx]["open"] / base - 1.0,
        "volume_z": vol_z,
    }


def build_reactions(limit: int = 500) -> int:
    with pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT a.published_at, t.ticker, a.id
            FROM news_articles a
            JOIN news_tickers t ON t.article_id = a.id
            JOIN instruments i ON i.ticker = t.ticker
            WHERE a.published_at >= now() - interval '400 days'
            ORDER BY a.published_at DESC
            LIMIT %s
            """,
            (limit,),
        ).fetchall()

    built = 0
    market_cache: dict[str, list[dict]] = {}
    for published_at, ticker, article_id in rows:
        end = published_at.date().isoformat()
        start = (published_at - timedelta(days=500)).date().isoformat()
        try:
            wide = _candles(ticker, "TQBR", start, end)
        except Exception as exc:  # noqa: BLE001 - one ticker must not break
            logger.warning("candles %s: %s", ticker, exc)
            continue
        if not wide:
            continue
        if start not in market_cache:
            market_cache[start] = _market(start, end)
        result = _reaction(wide, market_cache[start], published_at)
        if result is None:
            continue
        with pool().connection() as conn:
            conn.execute(
                """
                INSERT INTO news_reactions
                    (article_id, ticker, t0, ret_1d, ret_3d, ret_5d,
                     abnormal_ret, volume_z, mfe, mae, gap, costs)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (article_id, ticker) DO NOTHING
                """,
                (
                    article_id,
                    ticker,
                    result["t0"],
                    result["ret_1d"],
                    result["ret_3d"],
                    result["ret_5d"],
                    result["abnormal_ret"],
                    result["volume_z"],
                    result["mfe"],
                    result["mae"],
                    result["gap"],
                    COST,
                ),
            )
            conn.commit()
        built += 1
    return built


def _percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    k = (len(ordered) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def build_markers() -> int:
    with pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT n.event_type, n.sentiment, s.name AS source,
                   r.abnormal_ret
            FROM news_reactions r
            JOIN news_analysis n ON n.article_id = r.article_id
            JOIN news_articles a ON a.id = r.article_id
            LEFT JOIN news_sources s ON s.id = a.source_id
            WHERE r.abnormal_ret IS NOT NULL
            """
        ).fetchall()

    groups: dict[str, list[float]] = {}
    for event_type, sentiment, source, abnormal in rows:
        sentiment_label = (
            "pos" if sentiment > 0 else "neg" if sentiment < 0 else "neutral"
        )
        key = f"{event_type}|{sentiment_label}|{source or 'unknown'}"
        groups.setdefault(key, []).append(float(abnormal))

    written = 0
    with pool().connection() as conn:
        for key, values in groups.items():
            if len(values) < MIN_MARKER_N:
                continue
            mean = float(stat_mean(values))
            std = float(stdev(values)) if len(values) > 1 else 0.0
            t_stat = mean / (std / math.sqrt(len(values))) if std > 0 else 0.0
            event_type, sentiment_label, source = key.split("|", 2)
            conn.execute(
                """
                INSERT INTO event_markers
                    (marker_key, event_type, source, sentiment, n, mean, median,
                     t, hit, q05, q95, version)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1)
                ON CONFLICT (marker_key) DO UPDATE SET
                    n = EXCLUDED.n, mean = EXCLUDED.mean,
                    median = EXCLUDED.median, t = EXCLUDED.t,
                    hit = EXCLUDED.hit, q05 = EXCLUDED.q05, q95 = EXCLUDED.q95,
                    version = event_markers.version + 1,
                    updated_at = now()
                """,
                (
                    key,
                    event_type,
                    source,
                    sentiment_label,
                    len(values),
                    mean,
                    float(stat_median(values)),
                    t_stat,
                    sum(1 for v in values if v > 0) / len(values),
                    _percentile(values, 5),
                    _percentile(values, 95),
                ),
            )
            written += 1
        conn.commit()
    return written


def build_signals(limit: int = 500, min_edge: float = 0.001) -> int:
    with pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT a.id, t.ticker, n.event_type, n.sentiment, s.name AS source,
                   r.abnormal_ret
            FROM news_articles a
            JOIN news_tickers t ON t.article_id = a.id
            JOIN news_analysis n ON n.article_id = a.id
            LEFT JOIN news_sources s ON s.id = a.source_id
            LEFT JOIN news_reactions r
              ON r.article_id = a.id AND r.ticker = t.ticker
            ORDER BY a.published_at DESC
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
        markers = {
            row[0]: row
            for row in conn.execute(
                "SELECT marker_key, mean, t, n FROM event_markers"
            ).fetchall()
        }

        written = 0
        for article_id, ticker, event_type, sentiment, source, _abnormal in rows:
            sentiment_label = (
            "pos" if sentiment > 0 else "neg" if sentiment < 0 else "neutral"
        )
            key = f"{event_type}|{sentiment_label}|{source or 'unknown'}"
            marker = markers.get(key)
            if marker is None:
                continue
            _key, mean, t_stat, n = marker
            edge = float(mean) - COST
            if abs(edge) < min_edge:
                continue
            side = "buy" if edge > 0 else "sell"
            confidence = min(1.0, abs(float(t_stat)) / 3.0)
            conn.execute(
                """
                INSERT INTO signals
                    (article_id, ticker, side, expected_edge, confidence,
                     horizon, marker_key)
                VALUES (%s, %s, %s, %s, %s, '1d', %s)
                ON CONFLICT (article_id, ticker, side) DO NOTHING
                """,
                (article_id, ticker, side, edge, confidence, key),
            )
            written += 1
        conn.commit()
    return written
