from __future__ import annotations

from trading_news.news.moex_events import fetch_dividends

PAYLOAD = {
    "dividends": {
        "columns": ["registrydate", "value", "currencyid", "isin"],
        "data": [
            ["2026-07-10", 34.84, "RUB", "RU0009029540"],
            ["", None, "RUB", ""],
        ],
    }
}


def test_fetch_dividends_parses(monkeypatch) -> None:
    import httpx

    class Client:
        def get(self, url, params=None):
            return httpx.Response(200, json=PAYLOAD, request=httpx.Request("GET", url))

        def close(self):
            pass

    rows = fetch_dividends("SBER", Client())
    assert len(rows) == 2
    assert rows[0]["value"] == 34.84
    assert rows[0]["currencyid"] == "RUB"
    assert rows[1]["value"] is None
