from __future__ import annotations

from trading_news.nlp.classify import classify


def test_classify_dividend_positive() -> None:
    result = classify({"title": "Сбербанк выплатит рекордные дивиденды", "summary": ""})
    assert result["event_type"] == "dividend"
    assert result["sentiment"] == 1
    assert result["model"] == "rules-v2"


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


def test_classify_v2_weak_buckets_and_negation() -> None:
    from trading_news.nlp.classify import classify

    weak_pos = classify({"title": "Совет директоров одобрил расширение", "summary": ""})
    weak_neg = classify({"title": "Небольшое снижение выручки", "summary": ""})
    negated = classify({"title": "Компания без убытка", "summary": ""})
    assert 0 < weak_pos["score"] < 0.5
    assert -0.5 < weak_neg["score"] < 0
    assert negated["score"] >= 0
    assert weak_pos["model"] == "rules-v2"
