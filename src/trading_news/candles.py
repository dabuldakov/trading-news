"""Persistent price-candle cache.

Historical candles are immutable once closed, so they are stored locally and
only missing ranges are fetched from the upstream source.  The cache is
profile-scoped (``ru``/``crypto``) and deliberately separate from reactions:
reactions can be recomputed without touching the network.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta

from .config import settings
from .db import pool

Fetcher = Callable[[datetime, datetime], Iterable[dict]]


def _profile() -> str:
    return getattr(settings, "news_profile", "ru")


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _from_db_row(row) -> dict:
    ts, open_, high, low, close, volume, value = row
    return {
        "ts": ts,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "value": value,
    }


def _row(item: dict) -> dict:
    ts = item.get("ts", item.get("begin"))
    if isinstance(ts, (int, float)):
        ts = datetime.fromtimestamp(ts / 1000, tz=UTC)
    ts = _aware(ts)
    return {
        "ts": ts,
        "open": float(item.get("open") or item.get("close") or 0.0),
        "high": float(item.get("high") or item.get("close") or 0.0),
        "low": float(item.get("low") or item.get("close") or 0.0),
        "close": float(item.get("close") or 0.0),
        "volume": float(item.get("volume") or 0.0),
        "value": float(item.get("value") or 0.0),
    }


def load(
    ticker: str,
    timeframe: str,
    start: datetime,
    end: datetime,
) -> list[dict]:
    start, end = _aware(start), _aware(end)
    with pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT ts, open, high, low, close, volume, value
            FROM market_candles
            WHERE profile = %s AND ticker = %s AND timeframe = %s
              AND ts >= %s AND ts <= %s
            ORDER BY ts
            """,
            (_profile(), ticker, timeframe, start, end),
        ).fetchall()
    return [_from_db_row(row) for row in rows]


def save(
    ticker: str,
    timeframe: str,
    rows: Iterable[dict],
    *,
    source: str,
) -> int:
    payload = []
    for item in rows:
        row = _row(item)
        if row["close"] <= 0:
            continue
        payload.append(
            (
                _profile(),
                ticker,
                timeframe,
                row["ts"],
                row["open"],
                row["high"],
                row["low"],
                row["close"],
                row["volume"],
                row["value"],
                source,
            )
        )
    if not payload:
        return 0
    with pool().connection() as conn:
        with conn.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO market_candles
                    (profile, ticker, timeframe, ts, open, high, low, close,
                     volume, value, source)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (profile, ticker, timeframe, ts) DO UPDATE SET
                    open = EXCLUDED.open,
                    high = EXCLUDED.high,
                    low = EXCLUDED.low,
                    close = EXCLUDED.close,
                    volume = EXCLUDED.volume,
                    value = EXCLUDED.value,
                    source = EXCLUDED.source,
                    fetched_at = now()
                """,
                payload,
            )
        conn.commit()
    return len(payload)


def ensure(
    ticker: str,
    timeframe: str,
    start: datetime,
    end: datetime,
    fetcher: Fetcher,
    *,
    source: str,
    overlap: timedelta | None = None,
) -> list[dict]:
    """Return candles in ``[start, end]``, fetching only missing ranges."""
    start, end = _aware(start), _aware(end)
    if end < start:
        start, end = end, start
    cached = load(ticker, timeframe, start, end)
    if not cached:
        save(ticker, timeframe, fetcher(start, end), source=source)
        return load(ticker, timeframe, start, end)

    min_ts = min(row["ts"] for row in cached)
    max_ts = max(row["ts"] for row in cached)
    with pool().connection() as conn:
        last_fetch = conn.execute(
            """
            SELECT max(fetched_at) FROM market_candles
            WHERE profile = %s AND ticker = %s AND timeframe = %s
            """,
            (_profile(), ticker, timeframe),
        ).fetchone()[0]
    ranges: list[tuple[datetime, datetime]] = []
    fresh = (
        last_fetch is not None
        and datetime.now(UTC) - last_fetch < timedelta(hours=1)
    )
    # A daily bar can be later than midnight; don't refetch that small edge
    # every time while the cache was just refreshed.
    if start < min_ts and not (fresh and min_ts - start < timedelta(days=2)):
        ranges.append((start, min_ts - timedelta(seconds=1)))
    if end > max_ts and not fresh:
        overlap = overlap or timedelta(0)
        ranges.append((max_ts - overlap, end))
    for begin, finish in ranges:
        if finish < begin:
            continue
        save(ticker, timeframe, fetcher(begin, finish), source=source)
    return load(ticker, timeframe, start, end)


def latest(
    ticker: str,
    timeframe: str,
    limit: int,
    fetcher: Callable[[int], Iterable[dict]],
    *,
    source: str,
    max_age: timedelta | None = None,
) -> list[dict]:
    """Return latest candles, fetching the full window only when stale/short."""
    with pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT ts, open, high, low, close, volume, value
            FROM market_candles
            WHERE profile = %s AND ticker = %s AND timeframe = %s
            ORDER BY ts DESC
            LIMIT %s
            """,
            (_profile(), ticker, timeframe, limit),
        ).fetchall()
    cached = [_from_db_row(row) for row in reversed(rows)]
    fresh = bool(cached) and (
        max_age is None
        or datetime.now(UTC) - max(row["ts"] for row in cached) <= max_age
    )
    if len(cached) < limit or not fresh:
        save(ticker, timeframe, fetcher(limit), source=source)
        cached = load(
            ticker,
            timeframe,
            datetime.fromtimestamp(0, tz=UTC),
            datetime.now(UTC) + timedelta(days=1),
        )[-limit:]
    return cached
