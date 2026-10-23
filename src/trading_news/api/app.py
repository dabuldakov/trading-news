from __future__ import annotations

import hmac
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException

from ..config import settings
from ..db import close_pool, migrate, pool


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
        return [
            dict(zip(columns, row, strict=True)) for row in cursor.fetchall()
        ]


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
