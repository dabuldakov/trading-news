from __future__ import annotations

from trading_news.news.wayback import cdx_snapshots


def test_cdx_parse(monkeypatch) -> None:
    import httpx

    class Client:
        def get(self, url, params=None):
            payload = [
                ["urlkey", "timestamp", "original", "statuscode"],
                ["k", "20251005153120", "https://x/rss", "200"],
                ["k", "20251006035935", "https://x/rss", "404"],
            ]
            return httpx.Response(
                200, json=payload, request=httpx.Request("GET", url)
            )

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr(httpx, "Client", lambda *a, **k: Client())
    rows = cdx_snapshots("https://x/rss", "20251001", "20251010")
    assert len(rows) == 1
    assert rows[0]["timestamp"] == "20251005153120"
