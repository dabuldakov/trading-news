from __future__ import annotations

from trading_news.news.telegram import parse_channel

HTML = """
<html><body>
  <div class="tgme_widget_message" data-post="markettwits/123">
    <div class="tgme_widget_message_text">
      Сбербанк выплатит рекордные дивиденды
    </div>
    <time datetime="2026-10-23T07:15:00+00:00"></time>
  </div>
  <div class="tgme_widget_message" data-post="markettwits/124">
    <div class="tgme_widget_message_text">  </div>
    <time datetime="2026-10-23T07:20:00+00:00"></time>
  </div>
</body></html>
"""


def test_parse_channel() -> None:
    items = parse_channel(HTML, "markettwits")
    assert len(items) == 1
    assert items[0]["title"] == "Сбербанк выплатит рекордные дивиденды"
    assert items[0]["url"] == "https://t.me/markettwits/123"
    assert items[0]["published_at"].isoformat() == "2026-10-23T07:15:00+00:00"
    assert len(items[0]["hash"]) == 64
