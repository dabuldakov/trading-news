from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime

import httpx

from ..config import settings
from ..db import pool
from .moex import Issuer, MoexClient, Security

logger = logging.getLogger(__name__)


def _upsert_company(conn, issuer: Issuer, fallback_name: str) -> int:
    name = issuer.title or fallback_name
    row = conn.execute(
        """
        INSERT INTO companies (name, short_name, inn, updated_at)
        VALUES (%s, %s, %s, now())
        ON CONFLICT (name) DO UPDATE SET
            inn = COALESCE(NULLIF(EXCLUDED.inn, ''), companies.inn),
            updated_at = now()
        RETURNING id
        """,
        (name, name, issuer.inn),
    ).fetchone()
    return int(row[0])


def _upsert_instrument(
    conn, security: Security, company_id: int, broker: dict | None
) -> None:
    broker = broker or {}
    conn.execute(
        """
        INSERT INTO instruments
            (ticker, secid, isin, board, type, name, company_id, lot, currency,
             list_level, short_enabled, broker_available, source,
             last_seen_at, updated_at)
        VALUES (%s, %s, %s, %s, 'share', %s, %s, %s, %s, %s, %s, %s,
                'moex_iss', now(), now())
        ON CONFLICT (ticker) DO UPDATE SET
            isin = COALESCE(NULLIF(EXCLUDED.isin, ''), instruments.isin),
            name = COALESCE(NULLIF(EXCLUDED.name, ''), instruments.name),
            company_id = EXCLUDED.company_id,
            lot = EXCLUDED.lot,
            currency = EXCLUDED.currency,
            list_level = EXCLUDED.list_level,
            short_enabled = COALESCE(
                EXCLUDED.short_enabled, instruments.short_enabled
            ),
            broker_available = COALESCE(
                EXCLUDED.broker_available, instruments.broker_available
            ),
            source = EXCLUDED.source,
            last_seen_at = now(),
            updated_at = now()
        """,
        (
            security.ticker,
            security.ticker,
            security.isin,
            settings.moex_board,
            security.name,
            company_id,
            security.lot,
            security.currency,
            security.list_level,
            broker.get("short_enabled"),
            True if broker else None,
        ),
    )


def _upsert_alias(conn, company_id: int, alias: str, kind: str) -> None:
    alias = (alias or "").strip()
    if not alias:
        return
    conn.execute(
        """
        INSERT INTO aliases (company_id, alias, kind, lang, weight)
        VALUES (%s, %s, %s, 'ru', 1.0)
        ON CONFLICT (company_id, alias, kind) DO UPDATE SET
            weight = EXCLUDED.weight
        """,
        (company_id, alias, kind),
    )


def fetch_main_instruments() -> dict[str, dict]:
    """Optional broker-side list from the trading core's internal endpoint."""
    if not settings.main_api_url:
        return {}
    url = f"{settings.main_api_url.rstrip('/')}/internal/instruments"
    try:
        with httpx.Client(timeout=settings.http_timeout) as client:
            response = client.get(
                url, headers={"X-Service-Key": settings.main_service_key}
            )
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:  # noqa: BLE001 - optional integration
        logger.warning("main instruments unavailable: %s", exc)
        return {}
    if isinstance(payload, dict):
        items = payload.get("instruments") or []
    elif isinstance(payload, list):
        items = payload
    else:
        items = []
    out: dict[str, dict] = {}
    for item in items:
        ticker = str(item.get("ticker") or "").strip().upper()
        if ticker:
            out[ticker] = item
    return out


def sync_reference() -> dict:
    """Daily MOEX ISS sync: instruments, issuers, aliases (+ optional broker flags)."""
    client = MoexClient()
    started = datetime.now(UTC)
    with pool().connection() as conn:
        run = conn.execute(
            "INSERT INTO sync_runs (status) VALUES ('running') RETURNING id"
        ).fetchone()
        run_id = int(run[0])
        conn.commit()
        try:
            securities = client.securities()
            broker = fetch_main_instruments()
            companies: dict[str, int] = {}
            aliases = 0
            for index, security in enumerate(securities):
                issuer = client.description(security.ticker)
                key = issuer.title or security.name or security.ticker
                if key not in companies:
                    companies[key] = _upsert_company(conn, issuer, key)
                company_id = companies[key]
                _upsert_instrument(
                    conn, security, company_id, broker.get(security.ticker)
                )
                for alias, kind in (
                    (security.ticker, "ticker"),
                    (security.short_name, "short_name"),
                    (security.name, "name"),
                    (issuer.title, "issuer"),
                ):
                    _upsert_alias(conn, company_id, alias, kind)
                    aliases += 1
                time.sleep(0.1)
                if (index + 1) % 20 == 0:
                    conn.commit()
            conn.execute(
                """
                UPDATE instruments SET status = 'inactive'
                WHERE source = 'moex_iss' AND last_seen_at < %s
                """,
                (started,),
            )
            conn.execute(
                """
                UPDATE sync_runs
                SET status = 'ok', finished_at = now(), counts = %s
                WHERE id = %s
                """,
                (
                    json.dumps(
                        {
                            "securities": len(securities),
                            "companies": len(companies),
                            "aliases": aliases,
                        }
                    ),
                    run_id,
                ),
            )
            conn.commit()
            return {
                "securities": len(securities),
                "companies": len(companies),
                "aliases": aliases,
            }
        except Exception as exc:
            conn.execute(
                """
                UPDATE sync_runs
                SET status = 'error', finished_at = now(), error = %s
                WHERE id = %s
                """,
                (str(exc)[:1000], run_id),
            )
            conn.commit()
            raise


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(sync_reference())
