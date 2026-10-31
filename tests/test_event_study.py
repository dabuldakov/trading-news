from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from trading_news.db import pool
from trading_news.research.event_study import (
    _candles,
    _intraday_reaction,
    _reaction,
    build_intraday_reactions,
    build_markers,
    build_reactions,
    build_signals,
)
from trading_news.research.stats import event_group, marker_key, source_key


def _bar(
    day: int,
    close: float,
    high: float | None = None,
    low: float | None = None,
    value: float = 1000.0,
):
    return {
        "begin": datetime(2026, 1, day),
        "open": close,
        "high": high if high is not None else close,
        "low": low if low is not None else close,
        "close": close,
        "volume": 1000.0,
        "value": value,
    }


def test_reaction_positive_day() -> None:
    bars = [
        _bar(1, 100.0),
        _bar(2, 100.0),
        _bar(3, 105.0, high=106.0),
        _bar(4, 108.0),
        _bar(5, 110.0),
    ]
    result = _reaction(bars, bars, datetime(2026, 1, 3))
    assert result is not None
    assert result["t0"] == datetime(
        2026, 1, 3, tzinfo=ZoneInfo("Europe/Moscow")
    )
    assert result["ret_1d"] == pytest.approx(0.05)
    assert result["ret_3d"] == pytest.approx(0.10)
    assert result["mfe"] == pytest.approx(0.10)


def test_reaction_without_future_bars() -> None:
    bars = [_bar(1, 100.0), _bar(2, 100.0)]
    assert _reaction(bars, bars, datetime(2026, 1, 10)) is None


def test_reaction_abnormal_vs_market() -> None:
    bars = [
        _bar(1, 100.0),
        _bar(2, 100.0),
        _bar(3, 102.0),
        _bar(4, 103.0),
        _bar(5, 104.0),
        _bar(6, 105.0),
    ]
    market = [
        _bar(1, 100.0),
        _bar(2, 100.0),
        _bar(3, 101.0),
        _bar(4, 101.5),
        _bar(5, 102.0),
        _bar(6, 102.5),
    ]
    result = _reaction(bars, market, datetime(2026, 1, 3))
    assert result is not None
    assert result["ret_1d"] == pytest.approx(0.02)
    assert result["abnormal_ret"] == pytest.approx(0.01)
    assert result["abnormal_3d"] == pytest.approx(0.04 - 0.02)


def test_source_key_and_event_group() -> None:
    assert source_key("wayback:rbc") == "rbc"
    assert source_key("rbc") == "rbc"
    assert event_group("dividend") == "corporate"
    assert event_group("rate") == "macro"
    assert event_group("weird") == "other"
    assert marker_key("dividend", 1, "wayback:rbc") == "dividend|pos|rbc"
    assert marker_key("dividend", 1, "rbc", "event") == "dividend|pos"
    assert marker_key("rate", 0, "x", "group") == "macro|neutral"


def test_build_markers_and_signals() -> None:
    with pool().connection() as conn:
        source = conn.execute(
            "INSERT INTO news_sources (name, kind) VALUES ('rbc', 'rss') RETURNING id"
        ).fetchone()[0]
        company = conn.execute(
            "INSERT INTO companies (name) VALUES ('ПАО Сбербанк') RETURNING id"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO instruments (ticker, secid, company_id) "
            "VALUES ('SBER', 'SBER', %s)",
            (company,),
        )
        for index in range(5):
            article = conn.execute(
                """
                INSERT INTO news_articles (source_id, title, published_at, hash)
                VALUES (%s, 'Сбербанк выплатит дивиденды', now(), md5(%s))
                RETURNING id
                """,
                (source, str(index)),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO news_tickers (article_id, ticker) VALUES (%s, 'SBER')",
                (article,),
            )
            conn.execute(
                """
                INSERT INTO news_analysis (article_id, sentiment, event_type)
                VALUES (%s, 1, 'dividend')
                """,
                (article,),
            )
            value = (0.005, 0.008, 0.010, 0.012, 0.015)[index]
            conn.execute(
                """
                INSERT INTO news_reactions
                    (article_id, ticker, t0, abnormal_ret)
                VALUES (%s, 'SBER', now(), %s)
                """,
                (article, value),
            )
        conn.commit()

    assert build_markers() == 1
    assert build_signals() == 5

    with pool().connection() as conn:
        marker = conn.execute(
            "SELECT marker_key, n, mean FROM event_markers"
        ).fetchone()
        signal = conn.execute(
            "SELECT side, expected_edge, marker_key FROM signals LIMIT 1"
        ).fetchone()
    assert marker[0] == "dividend|pos|rbc"
    assert marker[1] == 5
    assert marker[2] == pytest.approx(0.01)
    assert signal[0] == "buy"
    assert signal[1] == pytest.approx(0.009)


def test_reaction_adv_and_extra_horizons() -> None:
    bars = [
        _bar(1, 100.0, value=500.0),
        _bar(2, 100.0, value=700.0),
        _bar(3, 102.0, value=900.0),
        _bar(4, 103.0),
        _bar(5, 104.0),
        _bar(6, 105.0),
        _bar(7, 106.0),
    ]
    result = _reaction(bars, bars, datetime(2026, 1, 3))
    assert result is not None
    assert result["adv_rub"] == pytest.approx(600.0)
    assert result["abnormal_3d"] == pytest.approx(0.0)
    assert result["abnormal_5d"] == pytest.approx(0.0)
    assert result["gap"] == pytest.approx(0.02)


def test_reaction_without_market_keeps_raw_returns() -> None:
    bars = [_bar(1, 100.0), _bar(2, 102.0), _bar(3, 103.0)]
    result = _reaction(bars, [], datetime(2026, 1, 2))
    assert result is not None
    assert result["ret_1d"] == pytest.approx(0.02)
    assert result["abnormal_ret"] is None
    assert result["abnormal_3d"] is None


def test_reaction_rejects_bad_base_and_missing_prefix() -> None:
    bars = [_bar(1, 0.0), _bar(2, 100.0), _bar(3, 101.0)]
    assert _reaction(bars, bars, datetime(2026, 1, 2)) is None
    assert _reaction(bars, bars, datetime(2025, 12, 31)) is None


CANDLE_COLUMNS = [
    "open",
    "close",
    "high",
    "low",
    "value",
    "volume",
    "begin",
    "end",
]


def _candle_rows(offset: int, count: int) -> list[list]:
    rows = []
    for index in range(count):
        day = datetime(2026, 1, 1) + timedelta(days=offset + index)
        close = 100.0 + offset + index
        rows.append(
            [
                close - 1,
                close,
                close + 1,
                close - 2,
                500.0 + offset + index,
                10.0,
                day.strftime("%Y-%m-%d %H:%M:%S"),
                day.strftime("%Y-%m-%d 23:59:59"),
            ]
        )
    return rows


@respx.mock
def test_candles_paginates_index_endpoint() -> None:
    route = respx.get(
        url__regex=r".*/index/boards/SNDX/securities/IMOEX/candles\.json"
    ).mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "candles": {
                        "columns": CANDLE_COLUMNS,
                        "data": _candle_rows(0, 500),
                    }
                },
            ),
            httpx.Response(
                200,
                json={
                    "candles": {
                        "columns": CANDLE_COLUMNS,
                        "data": _candle_rows(500, 1),
                    }
                },
            ),
        ]
    )

    bars = _candles("IMOEX", "SNDX", "2026-01-01", "2028-01-01", market="index")

    assert route.call_count == 2
    assert route.calls[0].request.url.params["start"] == "0"
    assert route.calls[1].request.url.params["start"] == "500"
    assert len(bars) == 501
    assert bars[0]["close"] == pytest.approx(100.0)
    assert bars[-1]["close"] == pytest.approx(600.0)
    assert bars[0]["value"] == pytest.approx(500.0)


@respx.mock
def test_candles_uses_shares_endpoint() -> None:
    route = respx.get(
        url__regex=r".*/shares/boards/TQBR/securities/SBER/candles\.json"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "candles": {
                    "columns": CANDLE_COLUMNS,
                    "data": _candle_rows(0, 2),
                }
            },
        )
    )

    bars = _candles("SBER", "TQBR", "2026-01-01", "2026-01-02")

    assert route.called
    assert [bar["close"] for bar in bars] == [100.0, 101.0]


def _seed_article(
    conn,
    source_id: int,
    *,
    ticker: str,
    published_at: datetime,
    title: str,
    event_type: str = "dividend",
    sentiment: float = 1.0,
    abnormal_ret: float | None = 0.01,
    ret_1d: float | None = 0.01,
    t0: datetime | None = None,
) -> int:
    article = conn.execute(
        """
        INSERT INTO news_articles (source_id, title, published_at, hash, status)
        VALUES (%s, %s, %s, md5(%s), 'linked')
        RETURNING id
        """,
        (source_id, title, published_at, f"{title}-{published_at.isoformat()}"),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO news_tickers (article_id, ticker) VALUES (%s, %s)",
        (article, ticker),
    )
    conn.execute(
        """
        INSERT INTO news_analysis (article_id, sentiment, event_type)
        VALUES (%s, %s, %s)
        """,
        (article, sentiment, event_type),
    )
    conn.execute(
        """
        INSERT INTO news_reactions
            (article_id, ticker, t0, ret_1d, abnormal_ret)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (article, ticker, t0 or published_at, ret_1d, abnormal_ret),
    )
    return article


@respx.mock
def test_build_reactions_uses_forward_bars_and_rebuilds() -> None:
    with pool().connection() as conn:
        source = conn.execute(
            "INSERT INTO news_sources (name, kind) VALUES ('rbc', 'rss') RETURNING id"
        ).fetchone()[0]
        company = conn.execute(
            "INSERT INTO companies (name) VALUES ('ПАО Сбербанк') RETURNING id"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO instruments (ticker, secid, company_id) "
            "VALUES ('SBER', 'SBER', %s)",
            (company,),
        )
        published = (datetime.now() - timedelta(days=5)).replace(
            hour=10, minute=0, second=0, microsecond=0
        )
        article = _seed_article(
            conn,
            source,
            ticker="SBER",
            published_at=published,
            title="Сбербанк отчитался",
        )
        conn.commit()

    def _response(request=None) -> httpx.Response:
        rows = []
        first_day = published.date() - timedelta(days=12)
        for index in range(42):
            day = datetime.combine(
                first_day + timedelta(days=index), datetime.min.time()
            )
            close = 100.0 + index
            rows.append(
                [
                    close,
                    close,
                    close + 1,
                    close - 1,
                    1_000_000.0,
                    1000.0,
                    day.strftime("%Y-%m-%d %H:%M:%S"),
                    day.strftime("%Y-%m-%d 23:59:59"),
                ]
            )
        return httpx.Response(
            200,
            json={"candles": {"columns": CANDLE_COLUMNS, "data": rows}},
        )

    respx.get(url__regex=r".*/shares/boards/TQBR/securities/SBER/candles\.json").mock(
        side_effect=_response
    )
    market_route = respx.get(
        url__regex=r".*/index/boards/SNDX/securities/IMOEX/candles\.json"
    ).mock(side_effect=_response)

    assert build_reactions(None, rebuild=True) == 1
    assert market_route.called

    with pool().connection() as conn:
        row = conn.execute(
            """
            SELECT ret_1d, abnormal_ret, abnormal_3d, abnormal_5d, adv_rub
            FROM news_reactions WHERE article_id = %s
            """,
            (article,),
        ).fetchone()
    assert row[0] is not None
    assert row[1] is not None
    assert row[2] is not None
    assert row[3] is not None
    assert row[4] == pytest.approx(1_000_000.0)
    assert row[0] > 0

    assert build_reactions(None, rebuild=True) == 1
    with pool().connection() as conn:
        count = conn.execute("SELECT count(*) FROM news_reactions").fetchone()[0]
    assert count == 1


def test_build_markers_merges_wayback_and_live_sources() -> None:
    with pool().connection() as conn:
        backfill = conn.execute(
            "INSERT INTO news_sources (name, kind) "
            "VALUES ('wayback:rbc', 'wayback') RETURNING id"
        ).fetchone()[0]
        live = conn.execute(
            "INSERT INTO news_sources (name, kind) VALUES ('rbc', 'rss') RETURNING id"
        ).fetchone()[0]
        company = conn.execute(
            "INSERT INTO companies (name) VALUES ('ПАО Сбербанк') RETURNING id"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO instruments (ticker, secid, company_id) "
            "VALUES ('SBER', 'SBER', %s)",
            (company,),
        )
        for index in range(5):
            _seed_article(
                conn,
                backfill,
                ticker="SBER",
                published_at=datetime(2025, 12, 1),
                title=f"backfill {index}",
                abnormal_ret=0.01,
            )
            _seed_article(
                conn,
                live,
                ticker="SBER",
                published_at=datetime(2026, 1, 1),
                title=f"live {index}",
                abnormal_ret=0.02,
            )
        conn.commit()

    assert build_markers() == 1
    with pool().connection() as conn:
        marker = conn.execute(
            "SELECT marker_key, n, mean FROM event_markers"
        ).fetchone()
    assert marker[0] == "dividend|pos|rbc"
    assert marker[1] == 10
    assert marker[2] == pytest.approx(0.015)


def test_build_signals_requires_significance_and_normalizes_source() -> None:
    with pool().connection() as conn:
        live = conn.execute(
            "INSERT INTO news_sources (name, kind) VALUES ('rbc', 'rss') RETURNING id"
        ).fetchone()[0]
        company = conn.execute(
            "INSERT INTO companies (name) VALUES ('ПАО Сбербанк') RETURNING id"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO instruments (ticker, secid, company_id) "
            "VALUES ('SBER', 'SBER', %s)",
            (company,),
        )
        _seed_article(
            conn,
            live,
            ticker="SBER",
            published_at=datetime(2026, 1, 1),
            title="Сбербанк: дивиденды",
            abnormal_ret=0.02,
        )
        conn.execute(
            """
            INSERT INTO event_markers
                (marker_key, event_type, source, sentiment, n, mean, t)
            VALUES ('dividend|pos|rbc', 'dividend', 'rbc', 'pos', 10, 0.02, 1.0)
            """
        )
        conn.commit()

    assert build_signals(min_t=2.0) == 0
    with pool().connection() as conn:
        conn.execute("UPDATE event_markers SET t = 3.0")
        conn.commit()
    assert build_signals(min_t=2.0) == 1
    with pool().connection() as conn:
        signal = conn.execute("SELECT side, marker_key FROM signals").fetchone()
    assert signal[0] == "buy"
    assert signal[1] == "dividend|pos|rbc"


def _minute_bar(when: datetime, price: float) -> dict:
    return {
        "begin": when,
        "open": price,
        "high": price,
        "low": price,
        "close": price,
        "volume": 100.0,
        "value": 1000.0,
    }


def test_intraday_reaction_horizons() -> None:
    start = datetime(2026, 1, 5, 10, 0)
    bars = [
        _minute_bar(start + timedelta(minutes=index), 100.0 + index)
        for index in range(90)
    ]
    result = _intraday_reaction(bars, start)
    assert result is not None
    assert result["ret_1m"] == pytest.approx(0.0)
    assert result["ret_5m"] == pytest.approx(0.04)
    assert result["ret_30m"] == pytest.approx(0.29)
    assert result["ret_1h"] == pytest.approx(0.59)


@respx.mock
def test_build_intraday_reactions_updates_rows() -> None:
    published = datetime.now(ZoneInfo("Europe/Moscow")).replace(
        hour=10, minute=0, second=0, microsecond=0
    ) - timedelta(days=1)
    with pool().connection() as conn:
        source = conn.execute(
            "INSERT INTO news_sources (name, kind) VALUES ('rbc', 'rss') RETURNING id"
        ).fetchone()[0]
        company = conn.execute(
            "INSERT INTO companies (name) VALUES ('ПАО Сбербанк') RETURNING id"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO instruments (ticker, secid, company_id) "
            "VALUES ('SBER', 'SBER', %s)",
            (company,),
        )
        article = _seed_article(
            conn,
            source,
            ticker="SBER",
            published_at=published,
            title="Сбербанк: интрадей",
        )
        conn.commit()

    day = published.date()
    rows = []
    for index in range(240):
        when = datetime.combine(day, datetime.min.time()) + timedelta(
            hours=9, minutes=index
        )
        price = 100.0 + index * 0.01
        rows.append(
            [
                price,
                price,
                price,
                price,
                1000.0,
                100.0,
                when.strftime("%Y-%m-%d %H:%M:%S"),
                (when + timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S"),
            ]
        )
    respx.get(url__regex=r".*/shares/boards/TQBR/securities/SBER/candles\.json").mock(
        return_value=httpx.Response(
            200,
            json={"candles": {"columns": CANDLE_COLUMNS, "data": rows}},
        )
    )

    assert build_intraday_reactions(days=7, limit=10) == 1
    with pool().connection() as conn:
        row = conn.execute(
            "SELECT ret_1m, ret_5m, ret_30m, ret_1h FROM news_reactions "
            "WHERE article_id = %s",
            (article,),
        ).fetchone()
    assert row[0] is not None
    assert row[1] is not None
    assert row[2] is not None
    assert row[3] is not None


def test_build_signals_marks_recent_consistency_validated() -> None:
    now = datetime.now()
    with pool().connection() as conn:
        source = conn.execute(
            "INSERT INTO news_sources (name, kind) VALUES ('rbc', 'rss') RETURNING id"
        ).fetchone()[0]
        company = conn.execute(
            "INSERT INTO companies (name) VALUES ('ПАО Сбербанк') RETURNING id"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO instruments (ticker, secid, company_id) "
            "VALUES ('SBER', 'SBER', %s)",
            (company,),
        )
        for index in range(12):
            _seed_article(
                conn,
                source,
                ticker="SBER",
                published_at=now - timedelta(days=3),
                title=f"recent {index}",
                abnormal_ret=0.015 if index % 2 else 0.025,
                t0=now - timedelta(days=2),
            )
        _seed_article(
            conn,
            source,
            ticker="SBER",
            published_at=now - timedelta(days=1),
            title="target",
            abnormal_ret=0.02,
            t0=now - timedelta(days=1),
        )
        conn.execute(
            """
            INSERT INTO event_markers
                (marker_key, event_type, source, sentiment, n, mean, t)
            VALUES ('dividend|pos|rbc', 'dividend', 'rbc', 'pos', 12, 0.02, 3.0)
            """
        )
        conn.commit()

    assert build_signals(min_t=2.0) == 13
    with pool().connection() as conn:
        count, all_valid = conn.execute(
            "SELECT count(*), bool_and(validated) FROM signals"
        ).fetchone()
    assert count == 13
    assert all_valid is True


@respx.mock
def test_cached_candles_preserve_moscow_time() -> None:
    route = respx.get(
        url__regex=r".*/shares/boards/TQBR/securities/SBER/candles\.json"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "candles": {
                    "columns": CANDLE_COLUMNS,
                    "data": [
                        [99.0, 100.0, 101.0, 98.0, 500.0, 10.0,
                         "2026-01-05 10:00:00", "2026-01-05 18:39:59"],
                    ],
                }
            },
        )
    )
    first = _candles("SBER", "TQBR", "2026-01-05", "2026-01-05")
    second = _candles("SBER", "TQBR", "2026-01-05", "2026-01-05")
    assert route.call_count == 1
    assert first[0]["begin"] == datetime(2026, 1, 5, 10, 0)
    assert second == first
