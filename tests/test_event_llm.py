from __future__ import annotations

from trading_news.nlp.event_llm import parse_event


def test_parse_event_handles_fence_and_alias() -> None:
    assert parse_event('```json\n{"event_type": "sanctions"}\n```') == "sanctions"


def test_parse_event_rejects_garbage_and_unknown() -> None:
    assert parse_event("no json") is None
    assert parse_event('{"event_type": "unknown"}') == "other"
    assert parse_event('{"event_type": "dividend"}') == "dividend"


def test_export_and_apply_event_llm() -> None:
    import json

    from trading_news.db import pool
    from trading_news.nlp.event_llm import apply_predictions, export_pending

    with pool().connection() as conn:
        source = conn.execute(
            "INSERT INTO news_sources (name, kind) VALUES ('rbc', 'rss') RETURNING id"
        ).fetchone()[0]
        article = conn.execute(
            """
            INSERT INTO news_articles (source_id, title, published_at, hash)
            VALUES (%s, 'Сбербанк: дивиденды', now(), md5('event-llm-test'))
            RETURNING id
            """,
            (source,),
        ).fetchone()[0]
        conn.execute("INSERT INTO news_analysis (article_id) VALUES (%s)", (article,))
        conn.commit()

    path = "/tmp/event_pending.jsonl"
    assert export_pending(path, limit=10) == 1
    prediction = json.loads(open(path, encoding="utf-8").read().splitlines()[0])
    prediction["event_type"] = "dividend"
    pred_path = "/tmp/event_predictions.jsonl"
    with open(pred_path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(prediction) + "\n")
    assert apply_predictions(pred_path) == 1
    with pool().connection() as conn:
        assert (
            conn.execute(
                "SELECT event_model FROM news_analysis WHERE article_id=%s", (article,)
            )
            .fetchone()[0]
            .startswith("llm:")
        )
