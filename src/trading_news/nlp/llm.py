"""LLM-разметка новостей через OpenAI-compatible /chat/completions.

Включается только если заданы LLM_BASE_URL/LLM_API_KEY/LLM_MODEL. Модель
возвращает sentiment (−1…1), event_type, materiality и surprise; поля пишутся
в те же news_analysis, что и rules-v2.
"""

from __future__ import annotations

import argparse
import json
import logging
import re

import httpx

from ..config import settings
from ..db import pool
from .classify import EVENTS

logger = logging.getLogger(__name__)

EVENT_TYPES = [name for name, _ in EVENTS] + ["other"]
PROMPT = f"""Ты финансовый аналитик MOEX. Верни строго JSON без пояснений:
{{"sentiment": число от -1 до 1, "event_type": одно из {EVENT_TYPES},
 "materiality": число от 0 до 1, "surprise": число от -1 до 1}}
sentiment: влияние новости на цену акции/сектора; materiality: значимость;
surprise: неожиданность относительно ожиданий рынка."""


def available() -> bool:
    return bool(settings.llm_base_url and settings.llm_api_key and settings.llm_model)


def _parse(content: str) -> dict | None:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    try:
        score = max(-1.0, min(1.0, float(payload.get("sentiment"))))
        materiality = max(0.0, min(1.0, float(payload.get("materiality", 0.0))))
        surprise = max(-1.0, min(1.0, float(payload.get("surprise", 0.0))))
    except (TypeError, ValueError):
        return None
    event_type = str(payload.get("event_type") or "other")
    if event_type not in EVENT_TYPES:
        event_type = "other"
    if score >= 0.15:
        sentiment = 1
    elif score <= -0.15:
        sentiment = -1
    else:
        sentiment = 0
    return {
        "sentiment": sentiment,
        "score": round(score, 4),
        "event_type": event_type,
        "materiality": round(materiality, 4),
        "surprise": round(surprise, 4),
        "model": f"llm:{settings.llm_model}",
    }


def classify_llm(title: str, summary: str = "") -> dict | None:
    if not available():
        raise RuntimeError("LLM не настроен: нужны LLM_BASE_URL/LLM_API_KEY/LLM_MODEL")
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": settings.llm_model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": PROMPT},
            {
                "role": "user",
                "content": f"Заголовок: {title}\nЛид: {summary}",
            },
        ],
    }
    try:
        response = httpx.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {settings.llm_api_key}"},
            timeout=60.0,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
    except Exception as exc:  # noqa: BLE001 - одна статья не рвёт batch
        logger.warning("llm: %s", str(exc)[:200])
        return None
    return _parse(str(content))


def classify_llm_pending(limit: int = 100) -> int:
    """Разметить статьи LLM (по умолчанию — только с реакциями)."""
    with pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT a.id, a.title, COALESCE(a.summary, '')
            FROM news_articles a
            JOIN news_analysis n ON n.article_id = a.id
            JOIN news_reactions r ON r.article_id = a.id
            WHERE n.model NOT LIKE 'llm:%%'
            ORDER BY a.published_at DESC
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
    updated = 0
    with pool().connection() as conn:
        for article_id, title, summary in rows:
            result = classify_llm(title, summary)
            if result is None:
                continue
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
            updated += 1
            if updated % 50 == 0:
                conn.commit()
                logger.info("llm: %s/%s", updated, len(rows))
        conn.commit()
    return updated


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="LLM news classifier")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    print({"updated": classify_llm_pending(args.limit)})
