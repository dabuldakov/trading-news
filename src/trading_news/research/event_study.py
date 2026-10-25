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
from collections import defaultdict
from datetime import datetime, timedelta
from statistics import mean as stat_mean
from statistics import median as stat_median
from statistics import pstdev, stdev
from zoneinfo import ZoneInfo

import httpx

from ..config import settings
from ..db import pool
from .stats import marker_key, sentiment_label, source_key
from .stats import stats as marker_stats

logger = logging.getLogger(__name__)

HORIZONS = (1, 3, 5)
COST = 0.001  # 10 bp round trip (commission + spread estimate)
MIN_MARKER_N = 5
FORWARD_DAYS = 40
PAGE = 500
INTRADAY_HORIZONS = (1, 5, 30, 60)
INTRADAY_DAYS = 120


def _candles(
    secid: str,
    board: str,
    start: str,
    end: str,
    *,
    market: str = "shares",
    interval: int = 24,
) -> list[dict]:
    if market == "index":
        path = (
            f"/engines/stock/markets/index/boards/{board}/securities/{secid}"
            "/candles.json"
        )
    else:
        path = (
            f"/engines/stock/markets/shares/boards/{board}/securities/{secid}"
            "/candles.json"
        )
    url = f"{settings.moex_iss_base.rstrip('/')}{path}"
    by_day: dict[datetime, dict] = {}
    offset = 0
    with httpx.Client(timeout=settings.http_timeout) as client:
        for _ in range(50):
            params = {
                "from": start,
                "till": end,
                "interval": interval,
                "iss.meta": "off",
                "start": offset,
            }
            response = client.get(url, params=params)
            response.raise_for_status()
            block = response.json().get("candles") or {}
            columns = block.get("columns") or []
            index = {name: i for i, name in enumerate(columns)}
            batch = block.get("data") or []
            if not batch:
                break
            for row in batch:
                begin = datetime.fromisoformat(str(row[index["begin"]]))
                by_day[begin] = {
                    "begin": begin,
                    "open": float(row[index["open"]]),
                    "high": float(row[index["high"]]),
                    "low": float(row[index["low"]]),
                    "close": float(row[index["close"]]),
                    "volume": float(row[index["volume"]]),
                    "value": (
                        float(row[index["value"]]) if "value" in index else 0.0
                    ),
                }
            if len(batch) < PAGE:
                break
            offset += len(batch)
    return [by_day[key] for key in sorted(by_day)]


def _market(start: str, end: str) -> list[dict]:
    return _candles("IMOEX", "SNDX", start, end, market="index")


def _at_or_after(bars: list[dict], published_at: datetime) -> int | None:
    return next(
        (i for i, bar in enumerate(bars) if bar["begin"] >= published_at),
        None,
    )


def _reaction(bars: list[dict], market: list[dict], published_at: datetime):
    if published_at.tzinfo is not None:
        published_at = published_at.astimezone(ZoneInfo("Europe/Moscow")).replace(
            tzinfo=None
        )
    idx = _at_or_after(bars, published_at)
    if idx is None or idx < 1:
        return None
    base = bars[idx - 1]["close"]
    if base <= 0:
        return None

    def ret(horizon: int) -> float | None:
        pos = idx + horizon - 1
        if pos >= len(bars):
            return None
        return bars[pos]["close"] / base - 1.0

    def market_ret(horizon: int) -> float | None:
        mbase = _at_or_after(market, published_at)
        if mbase is None or mbase < 1:
            return None
        pos = mbase + horizon - 1
        if pos >= len(market) or market[mbase - 1]["close"] <= 0:
            return None
        return market[pos]["close"] / market[mbase - 1]["close"] - 1.0

    returns = {h: ret(h) for h in HORIZONS}
    market_returns = {h: market_ret(h) for h in HORIZONS}

    def abnormal(horizon: int) -> float | None:
        if returns[horizon] is None or market_returns[horizon] is None:
            return None
        return returns[horizon] - market_returns[horizon]

    end = min(idx + max(HORIZONS) - 1, len(bars) - 1)
    window = bars[idx : end + 1]
    highs = [bar["high"] for bar in window]
    lows = [bar["low"] for bar in window]
    volumes = [bar["volume"] for bar in bars[max(0, idx - 20) : idx]]
    vol_z = 0.0
    if len(volumes) >= 5 and pstdev(volumes) > 0:
        vol_z = float((bars[idx]["volume"] - stat_mean(volumes)) / pstdev(volumes))

    prior = bars[max(0, idx - 20) : idx]
    adv = (
        stat_mean([bar.get("value", 0.0) for bar in prior])
        if prior and any(bar.get("value", 0.0) for bar in prior)
        else None
    )

    return {
        "t0": bars[idx]["begin"],
        "ret_1d": returns[1],
        "ret_3d": returns[3],
        "ret_5d": returns[5],
        "abnormal_ret": abnormal(1),
        "abnormal_3d": abnormal(3),
        "abnormal_5d": abnormal(5),
        "mfe": max(highs) / base - 1.0,
        "mae": min(lows) / base - 1.0,
        "gap": bars[idx]["open"] / base - 1.0,
        "volume_z": vol_z,
        "adv_rub": adv,
    }


def build_reactions(
    limit: int | None = 500,
    *,
    rebuild: bool = False,
    days: int = 400,
) -> int:
    query = """
        SELECT a.published_at, t.ticker, a.id
        FROM news_articles a
        JOIN news_tickers t ON t.article_id = a.id
        JOIN instruments i ON i.ticker = t.ticker
        WHERE a.published_at >= now() - (%s || ' days')::interval
        ORDER BY a.published_at DESC
    """
    params: list = [days]
    if limit is not None:
        query += " LIMIT %s"
        params.append(limit)
    with pool().connection() as conn:
        rows = conn.execute(query, params).fetchall()
    if not rows:
        return 0

    by_ticker: dict[str, list[tuple[datetime, int]]] = defaultdict(list)
    for published_at, ticker, article_id in rows:
        by_ticker[ticker].append((published_at, article_id))

    first = min(row[0] for row in rows) - timedelta(days=500)
    last = max(row[0] for row in rows) + timedelta(days=FORWARD_DAYS)
    try:
        market = _market(first.date().isoformat(), last.date().isoformat())
    except Exception as exc:  # noqa: BLE001 - market is optional for raw returns
        logger.warning("market candles: %s", exc)
        market = []

    payload: list[tuple] = []
    for number, (ticker, items) in enumerate(by_ticker.items(), start=1):
        start = (min(pub for pub, _ in items) - timedelta(days=500)).date().isoformat()
        end = (max(pub for pub, _ in items) + timedelta(days=FORWARD_DAYS)).date()
        try:
            bars = _candles(ticker, "TQBR", start, end.isoformat())
        except Exception as exc:  # noqa: BLE001 - one ticker must not break
            logger.warning("candles %s: %s", ticker, exc)
            continue
        if not bars:
            continue
        for published_at, article_id in items:
            result = _reaction(bars, market, published_at)
            if result is None:
                continue
            payload.append(
                (
                    article_id,
                    ticker,
                    result["t0"],
                    result["ret_1d"],
                    result["ret_3d"],
                    result["ret_5d"],
                    result["abnormal_ret"],
                    result["abnormal_3d"],
                    result["abnormal_5d"],
                    result["volume_z"],
                    result["mfe"],
                    result["mae"],
                    result["gap"],
                    result["adv_rub"],
                    COST,
                )
            )
        if number % 20 == 0:
            logger.info("reactions: %s/%s tickers", number, len(by_ticker))

    if not payload:
        return 0
    with pool().connection() as conn:
        if rebuild:
            conn.execute("DELETE FROM news_reactions")
        with conn.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO news_reactions
                    (article_id, ticker, t0, ret_1d, ret_3d, ret_5d,
                     abnormal_ret, abnormal_3d, abnormal_5d, volume_z,
                     mfe, mae, gap, adv_rub, costs)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s)
                ON CONFLICT (article_id, ticker) DO UPDATE SET
                    t0 = EXCLUDED.t0,
                    ret_1d = EXCLUDED.ret_1d,
                    ret_3d = EXCLUDED.ret_3d,
                    ret_5d = EXCLUDED.ret_5d,
                    abnormal_ret = EXCLUDED.abnormal_ret,
                    abnormal_3d = EXCLUDED.abnormal_3d,
                    abnormal_5d = EXCLUDED.abnormal_5d,
                    volume_z = EXCLUDED.volume_z,
                    mfe = EXCLUDED.mfe,
                    mae = EXCLUDED.mae,
                    gap = EXCLUDED.gap,
                    adv_rub = EXCLUDED.adv_rub,
                    costs = EXCLUDED.costs
                """,
                payload,
            )
        conn.commit()
    return len(payload)


def _intraday_reaction(bars: list[dict], published_at: datetime):
    if published_at.tzinfo is not None:
        published_at = published_at.astimezone(ZoneInfo("Europe/Moscow")).replace(
            tzinfo=None
        )
    idx = _at_or_after(bars, published_at)
    if idx is None:
        return None
    if idx >= 1:
        base = bars[idx - 1]["close"]
    else:
        base = bars[idx]["open"] or bars[idx]["close"]
    if base <= 0:
        return None
    result: dict[str, float | None] = {}
    for horizon in INTRADAY_HORIZONS:
        pos = idx + horizon - 1
        key = "ret_1h" if horizon == 60 else f"ret_{horizon}m"
        result[key] = bars[pos]["close"] / base - 1.0 if pos < len(bars) else None
    return result


def build_intraday_reactions(
    days: int = INTRADAY_DAYS,
    limit: int = 500,
) -> int:
    """Посчитать реакции 1м/5м/30м/1ч по минутным свечам MOEX.

    Берём только свежие публикации (``days`` дней), чтобы не тянуть минутки за
    весь бэкфилл. Свечи кэшируются по (тикер, дата) — новостей на бумагу в один
    день обычно несколько.
    """
    with pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT a.published_at, r.article_id, r.ticker
            FROM news_reactions r
            JOIN news_articles a ON a.id = r.article_id
            WHERE a.published_at >= now() - (%s || ' days')::interval
            ORDER BY a.published_at DESC
            LIMIT %s
            """,
            (days, limit),
        ).fetchall()

    cache: dict[tuple[str, str], list[dict]] = {}
    updates: list[tuple] = []
    for published_at, article_id, ticker in rows:
        local = published_at.astimezone(ZoneInfo("Europe/Moscow"))
        day = local.date()
        key = (ticker, day.isoformat())
        if key not in cache:
            try:
                cache[key] = _candles(
                    ticker,
                    "TQBR",
                    day.isoformat(),
                    (day + timedelta(days=3)).isoformat(),
                    interval=1,
                )
            except Exception as exc:  # noqa: BLE001 - one ticker must not break
                logger.warning("intraday %s %s: %s", ticker, day, exc)
                cache[key] = []
        bars = cache[key]
        if not bars:
            continue
        result = _intraday_reaction(bars, published_at)
        if result is None:
            continue
        updates.append(
            (
                result.get("ret_1m"),
                result.get("ret_5m"),
                result.get("ret_30m"),
                result.get("ret_1h"),
                article_id,
                ticker,
            )
        )
        if len(updates) % 200 == 0:
            logger.info("intraday: %s updates", len(updates))

    if not updates:
        return 0
    with pool().connection() as conn:
        with conn.cursor() as cursor:
            cursor.executemany(
                """
                UPDATE news_reactions
                SET ret_1m = %s, ret_5m = %s, ret_30m = %s, ret_1h = %s
                WHERE article_id = %s AND ticker = %s
                """,
                updates,
            )
        conn.commit()
    return len(updates)


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
                   COALESCE(r.abnormal_ret, r.ret_1d) AS abnormal_ret
            FROM news_reactions r
            JOIN news_analysis n ON n.article_id = r.article_id
            JOIN news_articles a ON a.id = r.article_id
            LEFT JOIN news_sources s ON s.id = a.source_id
            WHERE COALESCE(r.abnormal_ret, r.ret_1d) IS NOT NULL
            """
        ).fetchall()

    groups: dict[str, list[float]] = {}
    for event_type, sentiment, source, abnormal in rows:
        key = f"{event_type}|{sentiment_label(sentiment)}|{source_key(source)}"
        groups.setdefault(key, []).append(float(abnormal))

    written = 0
    with pool().connection() as conn:
        for key, values in groups.items():
            if len(values) < MIN_MARKER_N:
                continue
            mean = float(stat_mean(values))
            std = float(stdev(values)) if len(values) > 1 else 0.0
            t_stat = mean / (std / math.sqrt(len(values))) if std > 0 else 0.0
            event_type, sentiment_label_value, source = key.split("|", 2)
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
                    sentiment_label_value,
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


def _recent_markers(days: int) -> dict[str, dict]:
    with pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT n.event_type, n.sentiment, s.name AS source,
                   COALESCE(r.abnormal_ret, r.ret_1d) AS reaction
            FROM news_reactions r
            JOIN news_analysis n ON n.article_id = r.article_id
            JOIN news_articles a ON a.id = r.article_id
            LEFT JOIN news_sources s ON s.id = a.source_id
            WHERE r.t0 >= now() - (%s || ' days')::interval
              AND COALESCE(r.abnormal_ret, r.ret_1d) IS NOT NULL
            """,
            (days,),
        ).fetchall()
    groups: dict[str, list[float]] = {}
    for event_type, sentiment, source, reaction in rows:
        key = marker_key(event_type, sentiment, source)
        groups.setdefault(key, []).append(float(reaction))
    return {key: marker_stats(values) for key, values in groups.items()}


def build_signals(
    limit: int = 500,
    min_edge: float = 0.001,
    *,
    min_t: float = 2.0,
    recent_days: int = 180,
    min_recent_n: int = 10,
    min_recent_t: float = 2.0,
) -> int:
    recent = _recent_markers(recent_days)
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
            key = marker_key(event_type, sentiment, source)
            marker = markers.get(key)
            if marker is None:
                continue
            _key, mean, t_stat, n = marker
            if n < MIN_MARKER_N or abs(float(t_stat)) < min_t:
                continue
            edge = float(mean) - COST
            if abs(edge) < min_edge:
                continue
            side = "buy" if edge > 0 else "sell"
            confidence = min(1.0, abs(float(t_stat)) / 3.0)
            recent_marker = recent.get(key)
            validated = bool(
                recent_marker
                and recent_marker["n"] >= min_recent_n
                and abs(recent_marker["t"]) >= min_recent_t
                and (recent_marker["mean"] > 0) == (mean > 0)
                and abs(recent_marker["mean"] - COST) >= min_edge
            )
            conn.execute(
                """
                INSERT INTO signals
                    (article_id, ticker, side, expected_edge, confidence,
                     horizon, marker_key, validated)
                VALUES (%s, %s, %s, %s, %s, '1d', %s, %s)
                ON CONFLICT (article_id, ticker, side) DO UPDATE SET
                    expected_edge = EXCLUDED.expected_edge,
                    confidence = EXCLUDED.confidence,
                    marker_key = EXCLUDED.marker_key,
                    validated = EXCLUDED.validated
                """,
                (article_id, ticker, side, edge, confidence, key, validated),
            )
            written += 1
        conn.commit()
    return written
