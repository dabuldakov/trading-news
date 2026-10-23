"""News classification. Phase 3: rubert sentiment + LLM event/materiality.

For now this is a deterministic stub so the pipeline (status transitions,
tables) works end to end; the model is plugged in behind the same interface.
"""

from __future__ import annotations

import logging

from ..db import pool

logger = logging.getLogger(__name__)


def classify(article: dict) -> dict:
    return {
        "sentiment": 0.0,
        "event_type": "other",
        "materiality": 0.0,
        "model": "stub",
    }


def classify_pending(limit: int = 50) -> int:
    with pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT id, title, summary FROM news_articles
            WHERE status = 'new' ORDER BY published_at LIMIT %s
            """,
            (limit,),
        ).fetchall()
        for article_id, title, summary in rows:
            result = classify({"title": title, "summary": summary})
            conn.execute(
                """
                INSERT INTO news_analysis
                    (article_id, sentiment, score, event_type, materiality, model)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (article_id) DO NOTHING
                """,
                (
                    article_id,
                    result["sentiment"],
                    result["sentiment"],
                    result["event_type"],
                    result["materiality"],
                    result["model"],
                ),
            )
            conn.execute(
                "UPDATE news_articles SET status = 'analyzed' WHERE id = %s",
                (article_id,),
            )
        conn.commit()
    return len(rows)
