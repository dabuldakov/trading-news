from __future__ import annotations

import httpx
import pytest
import respx

from trading_news.config import settings
from trading_news.nlp.llm import _parse, available, classify_llm


def test_parse_handles_code_fence_and_clamps() -> None:
    result = _parse(
        '```json\n{"sentiment": -2, "event_type": "sanctions", '
        '"materiality": 2, "surprise": -3}\n```'
    )
    assert result is not None
    assert result["sentiment"] == -1
    assert result["score"] == -1.0
    assert result["event_type"] == "sanctions"
    assert result["materiality"] == 1.0
    assert result["surprise"] == -1.0


def test_parse_rejects_garbage() -> None:
    assert _parse("no json") is None
    assert _parse('{"sentiment": "x"}') is None


def test_available_requires_all_fields(monkeypatch) -> None:
    monkeypatch.setattr(settings, "llm_base_url", "")
    monkeypatch.setattr(settings, "llm_api_key", "k")
    monkeypatch.setattr(settings, "llm_model", "m")
    assert available() is False
    monkeypatch.setattr(settings, "llm_base_url", "http://llm")
    assert available() is True


@respx.mock
def test_classify_llm_parses_response(monkeypatch) -> None:
    monkeypatch.setattr(settings, "llm_base_url", "http://llm")
    monkeypatch.setattr(settings, "llm_api_key", "secret")
    monkeypatch.setattr(settings, "llm_model", "test-model")
    route = respx.post("http://llm/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"sentiment": 0.4, "event_type": '
                            '"dividend", "materiality": 0.5, '
                            '"surprise": 0.2}'
                        }
                    }
                ]
            },
        )
    )
    result = classify_llm("Сбербанк: дивиденды", "Рекордная выплата")
    assert route.called
    assert result is not None
    assert result["sentiment"] == 1
    assert result["score"] == pytest.approx(0.4)
    assert result["event_type"] == "dividend"
    assert result["model"] == "llm:test-model"
