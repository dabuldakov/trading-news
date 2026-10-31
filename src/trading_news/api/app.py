from __future__ import annotations

import hmac
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException

from ..config import settings
from ..db import close_pool, migrate, pool
from ..status import snapshot as status_snapshot


@asynccontextmanager
async def lifespan(app: FastAPI):
    migrate()
    yield
    close_pool()


app = FastAPI(title="trading-news", version="0.1.0", lifespan=lifespan)


def require_key(x_service_key: str = Header("", alias="X-Service-Key")) -> None:
    if not settings.service_key or not hmac.compare_digest(
        x_service_key, settings.service_key
    ):
        raise HTTPException(status_code=401, detail="invalid service key")


def _rows(conn, query: str, params: tuple | None = None) -> list[dict]:
    with conn.cursor() as cursor:
        cursor.execute(query, params)
        columns = [description.name for description in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


@app.get("/status")
def status() -> dict:
    return status_snapshot()


@app.get("/health")
def health() -> dict:
    with pool().connection() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok", "db": True}


@app.get("/internal/instruments", dependencies=[Depends(require_key)])
def instruments() -> dict:
    with pool().connection() as conn:
        rows = _rows(
            conn,
            """
            SELECT i.ticker, i.secid, i.isin, i.name, i.lot, i.currency,
                   i.short_enabled, i.broker_available, i.status,
                   c.name AS company
            FROM instruments i
            LEFT JOIN companies c ON c.id = i.company_id
            WHERE i.status = 'active'
            ORDER BY i.ticker
            """,
        )
    return {"instruments": rows}


@app.get("/internal/companies", dependencies=[Depends(require_key)])
def companies() -> dict:
    with pool().connection() as conn:
        rows = _rows(
            conn,
            """
            SELECT c.id, c.name, c.short_name, c.inn, c.sector,
                   count(i.ticker) AS instruments
            FROM companies c
            LEFT JOIN instruments i ON i.company_id = c.id
            GROUP BY c.id
            ORDER BY c.name
            """,
        )
    return {"companies": rows}


@app.post("/internal/sync", dependencies=[Depends(require_key)])
def trigger_sync() -> dict:
    from ..reference.sync import sync_reference

    return sync_reference()


@app.get("/internal/news", dependencies=[Depends(require_key)])
def news(since: str | None = None, limit: int = 50) -> dict:
    with pool().connection() as conn:
        rows = _rows(
            conn,
            """
            SELECT a.id, a.published_at, a.title, a.url,
                   s.name AS source,
                   COALESCE(
                       json_agg(
                           json_build_object(
                               'ticker', t.ticker,
                               'confidence', t.confidence
                           )
                       ) FILTER (WHERE t.ticker IS NOT NULL),
                       '[]'::json
                   ) AS tickers
            FROM news_articles a
            LEFT JOIN news_sources s ON s.id = a.source_id
            LEFT JOIN news_tickers t ON t.article_id = a.id
            WHERE (%s::timestamptz IS NULL OR a.published_at >= %s::timestamptz)
            GROUP BY a.id, s.name
            ORDER BY a.published_at DESC
            LIMIT %s
            """,
            (since, since, limit),
        )
    return {"news": rows}


@app.get("/internal/signals", dependencies=[Depends(require_key)])
def signals(
    since: str | None = None,
    limit: int = 100,
    include_unvalidated: bool = settings.signals_include_unvalidated,
) -> dict:
    with pool().connection() as conn:
        rows = _rows(
            conn,
            """
            SELECT s.id, s.article_id, s.ticker, s.side, s.expected_edge,
                   s.confidence, s.horizon, s.marker_key, s.validated,
                   s.created_at,
                   a.title, a.url, a.published_at,
                   COALESCE(NULLIF(a.summary, ''), left(a.body, 500)) AS summary,
                   src.name AS source,
                   n.event_type, n.sentiment,
                   t.confidence AS link_confidence,
                   t.method AS link_method,
                   t.relevance AS relevance,
                   t.direction AS direction,
                   (SELECT count(*) FROM news_tickers t2
                     WHERE t2.article_id = a.id) AS ticker_count
            FROM signals s
            JOIN news_articles a ON a.id = s.article_id
            JOIN news_analysis n ON n.article_id = a.id
            JOIN news_tickers t
              ON t.article_id = a.id AND t.ticker = s.ticker
            LEFT JOIN news_sources src ON src.id = a.source_id
            WHERE (%s::timestamptz IS NULL OR s.created_at >= %s::timestamptz)
              AND (%s OR s.validated)
            ORDER BY s.created_at DESC
            LIMIT %s
            """,
            (since, since, include_unvalidated, limit),
        )
        from ..news.link import aliases_for_tickers, link_is_valid

        aliases = aliases_for_tickers(
            conn, {str(row.get("ticker") or "") for row in rows}
        )
        rows = [
            row
            for row in rows
            if link_is_valid(
                f"{row.get('title') or ''} {row.get('summary') or ''}",
                str(row.get("ticker") or ""),
                row.get("link_method"),
                aliases.get(str(row.get("ticker") or "")),
            )
        ]
    return {"signals": rows}


@app.post("/internal/link", dependencies=[Depends(require_key)])
def run_link(limit: int = 500) -> dict:
    from ..news.link import link_pending

    return {"linked": link_pending(limit=limit)}
