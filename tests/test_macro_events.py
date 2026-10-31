from __future__ import annotations

import httpx
import respx

from trading_news.news import macro, moex_events


def test_parse_key_rate() -> None:
    html = "<table><tr><td>01.01.2026</td><td>16,50</td></tr></table>"
    rows = macro.parse_key_rate(html)
    assert rows[0]["value"] == 16.5


@respx.mock
def test_ingest_key_rate() -> None:
    respx.get(macro.CBR_KEY_RATE_URL).mock(
        return_value=httpx.Response(
            200,
            text="<table><tr><td>01.01.2026</td><td>16,50</td></tr></table>",
        )
    )
    result = macro.ingest_key_rate()
    assert result["rates"] == 1
    assert result["inserted"] == 1


@respx.mock
def test_fetch_and_ingest_dividends(monkeypatch) -> None:
    from trading_news.db import pool

    with pool().connection() as conn:
        company = conn.execute(
            "INSERT INTO companies (name) VALUES ('Сбербанк') RETURNING id"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO instruments (ticker, secid, company_id, status) "
            "VALUES ('SBER','SBER',%s,'active')",
            (company,),
        )
        conn.commit()
    respx.get("https://iss.moex.com/iss/securities/SBER/dividends.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "dividends": {
                    "columns": ["registrydate", "value", "currencyid"],
                    "data": [["2026-01-01", 10.0, "RUB"]],
                }
            },
        )
    )
    assert moex_events.fetch_dividends("SBER")[0]["value"] == 10.0
    result = moex_events.ingest_dividends(limit=1)
    assert result["inserted"] == 1
