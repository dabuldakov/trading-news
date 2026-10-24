"""News classification. v1 — deterministic rules; later — rubert/LLM behind
the same interface (sentiment, event_type, materiality, model)."""

from __future__ import annotations

import logging

from ..db import pool

logger = logging.getLogger(__name__)

POSITIVE = {
    "рост",
    "прибыл",
    "дивиденд",
    "выручк",
    "рекорд",
    "увелич",
    "повыс",
    "позитив",
    "контракт",
    "соглашен",
    "запуск",
    "расшир",
    "выкуп",
    "buyback",
    "улучш",
}

NEGATIVE = {
    "паден",
    "убыток",
    "снижен",
    "снизил",
    "санкц",
    "штраф",
    "иск",
    "авари",
    "сокращ",
    "отмен",
    "дефолт",
    "риск",
    "негатив",
    "проблем",
    "расследован",
    "запрет",
    "потер",
}

EVENTS: list[tuple[str, set[str]]] = [
    ("dividend", {"дивиденд"}),
    ("earnings", {"отчёт", "отчет", "прибыл", "выручк", "ebitda", "финансов"}),
    ("m&a", {"поглощ", "слиян", "сделк", "приобрет", "выкуп", "buyback"}),
    ("sanctions", {"санкц", "ограничен"}),
    ("rate", {"ключев", "ставк", "дкп", "банк россии"}),
    ("cpi", {"инфляц", "потребительск"}),
    ("gdp", {"ввп"}),
    ("budget", {"бюджет", "дефицит", "минфин", "офз"}),
    ("regulation", {"регулятор", "закон", "цб "}),
    ("contract", {"контракт", "соглашен", "поставк", "тендер"}),
    ("management", {"назнач", "увол", "глава", "гендир", "совет директор"}),
    ("production", {"добыч", "производств", "запуск", "мощност"}),
    ("rating", {"рейтинг", "прогноз"}),
]


def classify(article: dict) -> dict:
    text = f"{article.get('title', '')} {article.get('summary', '')}".lower()
    positive = sum(1 for word in POSITIVE if word in text)
    negative = sum(1 for word in NEGATIVE if word in text)
    if positive > negative:
        sentiment = 1
    elif negative > positive:
        sentiment = -1
    else:
        sentiment = 0
    total = positive + negative
    score = (positive - negative) / total if total else 0.0
    event_type = "other"
    for name, words in EVENTS:
        if any(word in text for word in words):
            event_type = name
            break
    return {
        "sentiment": sentiment,
        "score": score,
        "event_type": event_type,
        "materiality": min(1.0, total / 3),
        "model": "rules-v1",
    }


def classify_pending(limit: int = 100) -> int:
    with pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT a.id, a.title, COALESCE(a.summary, '')
            FROM news_articles a
            LEFT JOIN news_analysis n ON n.article_id = a.id
            WHERE a.status IN ('new', 'linked') AND n.article_id IS NULL
            ORDER BY a.published_at
            LIMIT %s
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
                ON CONFLICT (article_id) DO UPDATE SET
                    sentiment = EXCLUDED.sentiment,
                    score = EXCLUDED.score,
                    event_type = EXCLUDED.event_type,
                    materiality = EXCLUDED.materiality,
                    model = EXCLUDED.model
                """,
                (
                    article_id,
                    result["sentiment"],
                    result["score"],
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
