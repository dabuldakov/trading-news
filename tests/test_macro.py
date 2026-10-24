from __future__ import annotations

from trading_news.news.macro import parse_key_rate

HTML = """
<table>
  <tr><td>Дата</td><td>Ставка</td></tr>
  <tr><td>25.10.2026</td><td>16,50</td></tr>
  <tr><td>12.09.2026</td><td>17,00</td></tr>
  <tr><td>—</td><td>—</td></tr>
</table>
"""


def test_parse_key_rate() -> None:
    rows = parse_key_rate(HTML)
    assert len(rows) == 2
    assert rows[0]["value"] == 16.50
    assert rows[0]["date"].isoformat() == "2026-10-25T00:00:00+00:00"
    assert rows[1]["value"] == 17.00
