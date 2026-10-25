from __future__ import annotations

from trading_news.db import pool
from trading_news.reference.sectors import backfill_sectors, sector_for


def test_sector_for_handles_case_and_unknown() -> None:
    assert sector_for("gazp") == "oil_gas"
    assert sector_for(" SBER ") == "banks"
    assert sector_for("NOPE") == ""
    assert sector_for(None) == ""


def test_backfill_sectors_is_idempotent() -> None:
    with pool().connection() as conn:
        for ticker, name, sector in (
            ("GAZP", "Газпром", None),
            ("SMLT", "Самолет", None),
            ("LKOH", "Лукойл", "custom"),
        ):
            company = conn.execute(
                "INSERT INTO companies (name, sector) VALUES (%s, %s) "
                "RETURNING id",
                (name, sector),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO instruments (ticker, secid, company_id) "
                "VALUES (%s, %s, %s)",
                (ticker, ticker, company),
            )
        conn.commit()
        assert backfill_sectors(conn) == 2
        conn.commit()
        rows = dict(
            conn.execute(
                "SELECT i.ticker, c.sector FROM instruments i "
                "JOIN companies c ON c.id = i.company_id"
            ).fetchall()
        )
        assert rows == {"GAZP": "oil_gas", "SMLT": "real_estate", "LKOH": "custom"}
        assert backfill_sectors(conn) == 0
