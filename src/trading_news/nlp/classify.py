"""News classification. v2 — взвешенные правила + negation/materiality/surprise.

Sentiment непрерывный (score −1…1), поэтому появляются weak_neg/weak_pos, а не
только ±1. LLM-разметка — отдельно (``nlp.llm``), поверх тех же полей.
"""

from __future__ import annotations

import logging
import math
import re

from ..db import pool

logger = logging.getLogger(__name__)

POSITIVE: dict[str, float] = {
    "рекорд": 1.6,
    "дивиденд": 1.1,
    "прибыл": 1.1,
    "рост": 1.0,
    "выручк": 0.4,
    "увелич": 0.7,
    "повыс": 0.7,
    "позитив": 0.8,
    "контракт": 0.8,
    "соглашен": 0.6,
    "запуск": 0.6,
    "расшир": 0.5,
    "выкуп": 1.0,
    "buyback": 1.0,
    "улучш": 0.6,
    "превыс": 0.9,
    "ускор": 0.6,
    "стабильн": 0.4,
    "восстанов": 0.5,
    "подорож": 0.6,
}

NEGATIVE: dict[str, float] = {
    "паден": 1.0,
    "убыток": 1.3,
    "снижен": 0.8,
    "снизил": 0.8,
    "санкц": 1.6,
    "штраф": 1.1,
    "иск": 0.7,
    "авари": 1.2,
    "сокращ": 0.6,
    "отмен": 0.7,
    "дефолт": 1.6,
    "риск": 0.5,
    "негатив": 0.9,
    "проблем": 0.6,
    "расследован": 0.8,
    "запрет": 0.9,
    "потер": 0.9,
    "подешев": 0.6,
    "обвал": 1.4,
    "просроч": 1.0,
}

NEGATIONS = ("не", "без", "нет", "отсутств")

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


def _weighted(text: str, lexicon: dict[str, float], polarity: int) -> float:
    total = 0.0
    for word, weight in lexicon.items():
        start = 0
        while True:
            position = text.find(word, start)
            if position < 0:
                break
            prefix = text[max(0, position - 16) : position]
            negated = any(re.search(rf"\b{neg}\b", prefix) for neg in NEGATIONS)
            contribution = -weight * 0.8 if negated else weight
            total += polarity * contribution
            start = position + len(word)
    return total


def _surprise(text: str, score: float) -> float:
    values = [
        float(value.replace(",", "."))
        for value in re.findall(r"(\d+(?:[.,]\d+)?)\s*%", text)
    ]
    magnitude = min(1.0, max(values, default=0.0) / 20.0)
    if any(word in text for word in ("рекорд", "неожид", "впервые")):
        magnitude = min(1.0, magnitude + 0.3)
    if any(word in text for word in ("хуже прогноз", "ниже прогноз", "слабее прогноз")):
        magnitude = max(0.0, magnitude - 0.4)
    if "прогноз" in text and magnitude == 0:
        magnitude = 0.2
    if score > 0:
        return magnitude
    if score < 0:
        return -magnitude
    return 0.0


def classify(article: dict) -> dict:
    text = f"{article.get('title', '')} {article.get('summary', '')}".lower()
    positive = _weighted(text, POSITIVE, 1)
    negative = _weighted(text, NEGATIVE, -1)
    raw = positive + negative
    total = positive + abs(negative)
    score = math.tanh(raw / 2.0) if total else 0.0
    if score >= 0.15:
        sentiment = 1
    elif score <= -0.15:
        sentiment = -1
    else:
        sentiment = 0
    event_type = "other"
    for name, words in EVENTS:
        if any(word in text for word in words):
            event_type = name
            break
    return {
        "sentiment": sentiment,
        "score": round(score, 4),
        "event_type": event_type,
        "materiality": round(min(1.0, total / 5.0), 4),
        "surprise": round(_surprise(text, score), 4),
        "model": "rules-v2",
    }


def classify_pending(limit: int = 100) -> int:
    with pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT a.id, a.title, COALESCE(a.summary, '')
            FROM news_articles a
            LEFT JOIN news_analysis n ON n.article_id = a.id
            WHERE a.status IN ('new', 'linked', 'skipped')
              AND n.article_id IS NULL
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
                    (article_id, sentiment, score, event_type, materiality,
                     surprise, model)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (article_id) DO UPDATE SET
                    sentiment = EXCLUDED.sentiment,
                    score = EXCLUDED.score,
                    event_type = EXCLUDED.event_type,
                    materiality = EXCLUDED.materiality,
                    surprise = EXCLUDED.surprise,
                    model = EXCLUDED.model
                """,
                (
                    article_id,
                    result["sentiment"],
                    result["score"],
                    result["event_type"],
                    result["materiality"],
                    result["surprise"],
                    result["model"],
                ),
            )
            conn.execute(
                "UPDATE news_articles SET status = 'analyzed' WHERE id = %s",
                (article_id,),
            )
        conn.commit()
    return len(rows)


def reclassify_all(limit: int = 200000) -> int:
    """Пересчитать sentiment/event_type/score по текущим правилам."""
    with pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT n.article_id, a.title, COALESCE(a.summary, '')
            FROM news_analysis n
            JOIN news_articles a ON a.id = n.article_id
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
        for article_id, title, summary in rows:
            result = classify({"title": title, "summary": summary})
            conn.execute(
                """
                UPDATE news_analysis
                SET sentiment = %s, score = %s,
                    event_type = CASE WHEN event_model IS NULL
                                      THEN %s ELSE event_type END,
                    materiality = %s, surprise = %s, model = %s
                WHERE article_id = %s
                """,
                (
                    result["sentiment"],
                    result["score"],
                    result["event_type"],
                    result["materiality"],
                    result["surprise"],
                    result["model"],
                    article_id,
                ),
            )
        conn.commit()
    return len(rows)


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="reclassify news")
    parser.add_argument("--reclassify", action="store_true")
    args = parser.parse_args()
    if args.reclassify:
        print({"reclassified": reclassify_all()})
