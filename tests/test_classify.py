from __future__ import annotations

from trading_news.nlp.classify import classify


def test_classify_dividend_positive() -> None:
    result = classify(
        {"title": "Сбербанк выплатит рекордные дивиденды", "summary": ""}
    )
    assert result["event_type"] == "dividend"
    assert result["sentiment"] == 1
    assert result["model"] == "rules-v1"


def test_classify_sanctions_negative() -> None:
    result = classify({"title": "Новые санкции против компании", "summary": ""})
    assert result["event_type"] == "sanctions"
    assert result["sentiment"] == -1


def test_classify_neutral_other() -> None:
    result = classify({"title": "Погода в Москве", "summary": ""})
    assert result["sentiment"] == 0
    assert result["event_type"] == "other"


def test_classify_macro_rate() -> None:
    result = classify({"title": "ЦБ повысил ключевую ставку", "summary": ""})
    assert result["event_type"] == "rate"


def test_classify_macro_cpi() -> None:
    result = classify({"title": "Инфляция в России замедлилась", "summary": ""})
    assert result["event_type"] == "cpi"
