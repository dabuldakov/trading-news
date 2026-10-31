from __future__ import annotations

from datetime import UTC, datetime, timedelta

from trading_news.candles import ensure, latest, load, save


def _ts(day: int) -> datetime:
    return datetime(2026, 1, day, tzinfo=UTC)


def _bar(day: int, close: float, value: float = 1000.0) -> dict:
    return {
        "ts": _ts(day),
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 10.0,
        "value": value,
    }


def test_save_and_load_range() -> None:
    save("TEST", "1d", [_bar(1, 100.0)], source="test")
    rows = load("TEST", "1d", _ts(1), _ts(1))
    assert len(rows) == 1
    assert rows[0]["close"] == 100.0
    assert rows[0]["value"] == 1000.0


def test_ensure_does_not_refetch_cached_range() -> None:
    calls: list[tuple[datetime, datetime]] = []

    def fetch(start, end):
        calls.append((start, end))
        return [_bar(1, 100.0), _bar(2, 101.0)]

    first = ensure("TEST2", "1d", _ts(1), _ts(2), fetch, source="test")
    assert len(first) == 2
    assert len(calls) == 1

    second = ensure("TEST2", "1d", _ts(1), _ts(2), fetch, source="test")
    assert len(second) == 2
    assert len(calls) == 1


def test_ensure_fetches_only_new_tail() -> None:
    save("TEST3", "1d", [_bar(1, 100.0), _bar(2, 101.0)], source="test")
    calls: list[tuple[datetime, datetime]] = []

    def fetch(start, end):
        calls.append((start, end))
        return [_bar(3, 102.0)]

    rows = ensure(
        "TEST3",
        "1d",
        _ts(1),
        _ts(3),
        fetch,
        source="test",
        overlap=timedelta(0),
    )
    assert len(rows) == 3
    assert calls == [(_ts(2), _ts(3))]


def test_latest_uses_cache_when_full_and_fresh() -> None:
    save("TEST4", "1d", [_bar(day, 100.0 + day) for day in (1, 2, 3)], source="test")
    calls: list[int] = []

    def fetch(limit):
        calls.append(limit)
        return []

    rows = latest(
        "TEST4",
        "1d",
        3,
        fetch,
        source="test",
        max_age=None,
    )
    assert len(rows) == 3
    assert calls == []
