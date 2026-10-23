from __future__ import annotations

import httpx
import pytest
import respx

from trading_news.db import pool
from trading_news.reference.sync import sync_reference

SECURITIES = {
    "securities": {
        "columns": [
            "SECID",
            "SHORTNAME",
            "SECNAME",
            "LOTSIZE",
            "ISIN",
            "LISTLEVEL",
            "CURRENCYID",
        ],
        "data": [
            ["SBER", "Сбербанк", "Сбербанк ПАО", "10", "RU0009029540", "1", "RUB"],
        ],
    }
}

DESCRIPTION = {
    "description": {
        "columns": ["name", "title", "value"],
        "data": [
            ["emitent_title", "Эмитент", "ПАО Сбербанк"],
            ["emitent_inn", "ИНН", "7707083893"],
        ],
    }
}


@respx.mock
def test_sync_reference_upserts_instrument_company_alias() -> None:
    respx.get(url__regex=r".*/boards/TQBR/securities\.json.*").mock(
        return_value=httpx.Response(200, json=SECURITIES)
    )
    respx.get(url__regex=r".*/securities/SBER\.json.*").mock(
        return_value=httpx.Response(200, json=DESCRIPTION)
    )

    result = sync_reference()

    assert result == {"securities": 1, "companies": 1, "aliases": 4}
    with pool().connection() as conn:
        instrument = conn.execute(
            """
            SELECT ticker, isin, lot, company_id, status
            FROM instruments WHERE ticker = 'SBER'
            """
        ).fetchone()
        company = conn.execute(
            "SELECT name, inn FROM companies"
        ).fetchone()
        aliases = conn.execute("SELECT count(*) FROM aliases").fetchone()[0]
        run = conn.execute(
            "SELECT status FROM sync_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()

    assert instrument[0] == "SBER"
    assert instrument[1] == "RU0009029540"
    assert instrument[2] == 10
    assert instrument[4] == "active"
    assert company == ("ПАО Сбербанк", "7707083893")
    assert aliases == 4
    assert run[0] == "ok"


@respx.mock
def test_sync_reference_marks_missing_instruments_inactive() -> None:
    respx.get(url__regex=r".*/boards/TQBR/securities\.json.*").mock(
        return_value=httpx.Response(200, json=SECURITIES)
    )
    respx.get(url__regex=r".*/securities/SBER\.json.*").mock(
        return_value=httpx.Response(200, json=DESCRIPTION)
    )
    sync_reference()

    empty = {"securities": {"columns": SECURITIES["securities"]["columns"], "data": []}}
    respx.get(url__regex=r".*/boards/TQBR/securities\.json.*").mock(
        return_value=httpx.Response(200, json=empty)
    )
    sync_reference()

    with pool().connection() as conn:
        status = conn.execute(
            "SELECT status FROM instruments WHERE ticker = 'SBER'"
        ).fetchone()[0]
    assert status == "inactive"


@pytest.mark.parametrize("status", [500, 502])
@respx.mock
def test_sync_reference_records_error(status: int) -> None:
    respx.get(url__regex=r".*/boards/TQBR/securities\.json.*").mock(
        return_value=httpx.Response(status)
    )
    with pytest.raises(httpx.HTTPStatusError):
        sync_reference()
    with pool().connection() as conn:
        run = conn.execute(
            "SELECT status, error FROM sync_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert run[0] == "error"
    assert run[1]
