from __future__ import annotations

import logging
from pathlib import Path

from psycopg_pool import ConnectionPool

from .config import settings

logger = logging.getLogger(__name__)

_pool: ConnectionPool | None = None


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            settings.database_url, min_size=1, max_size=10, open=False
        )
        _pool.open()
        _pool.wait()
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def migrate() -> list[str]:
    """Apply SQL migrations from ``migrations_dir`` once each."""
    applied: list[str] = []
    with pool().connection() as conn:
        conn.execute("SELECT pg_advisory_lock(hashtext('trading_news_migrations'))")
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            conn.commit()
            done = {
                row[0]
                for row in conn.execute(
                    "SELECT version FROM schema_migrations"
                ).fetchall()
            }
            for path in sorted(Path(settings.migrations_dir).glob("*.sql")):
                if path.name in done:
                    continue
                logger.info("applying migration %s", path.name)
                with conn.transaction():
                    conn.execute(path.read_text())
                    conn.execute(
                        "INSERT INTO schema_migrations (version) VALUES (%s)",
                        (path.name,),
                    )
                applied.append(path.name)
        finally:
            conn.execute(
                "SELECT pg_advisory_unlock(hashtext('trading_news_migrations'))"
            )
            conn.commit()
    return applied
