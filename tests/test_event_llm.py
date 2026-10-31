from __future__ import annotations

from trading_news.nlp.event_llm import parse_event


def test_parse_event_handles_fence_and_alias() -> None:
    assert parse_event('```json\n{"event_type": "sanctions"}\n```') == "sanctions"


def test_parse_event_rejects_garbage_and_unknown() -> None:
    assert parse_event("no json") is None
    assert parse_event('{"event_type": "unknown"}') == "other"
    assert parse_event('{"event_type": "dividend"}') == "dividend"
