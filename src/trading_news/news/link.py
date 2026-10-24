from __future__ import annotations

import logging

from ..db import pool

logger = logging.getLogger(__name__)

# Слова, которые есть в названиях почти всех эмитентов и дают ложные совпадения.
GENERIC = {
    "пао",
    "оао",
    "зао",
    "ао",
    "акции",
    "акция",
    "банк",
    "россия",
    "группа",
    "компания",
    "холдинг",
    "гмк",
    "нк",
    "завод",
}


def link_pending(limit: int = 5000, min_similarity: float = 0.55) -> int:
    """Связать новые статьи с тикерами по алиасам компаний.

    Точное вхождение алиаса в заголовок/лид — confidence 0.95; иначе триграммы
    (`pg_trgm`, similarity ≥ ``min_similarity``). Общие слова из ``GENERIC``
    отбрасываются. Статья помечается ``linked`` (есть совпадения) или
    ``skipped`` (нет).
    """
    linked = 0
    with pool().connection() as conn:
        articles = conn.execute(
            """
            SELECT id, title, COALESCE(summary, '') FROM news_articles
            WHERE status IN ('new', 'skipped')
            ORDER BY published_at
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
        for article_id, title, summary in articles:
            text = f"{title} {summary}".strip()
            rows = conn.execute(
                """
                SELECT a.company_id, a.alias, i.ticker,
                       similarity(lower(a.alias), lower(%s)) AS sim,
                       (%s ILIKE '%%' || a.alias || '%%') AS exact
                FROM aliases a
                JOIN instruments i
                  ON i.company_id = a.company_id AND i.status = 'active'
                WHERE length(a.alias) >= 4
                ORDER BY sim DESC
                LIMIT 25
                """,
                (text, text),
            ).fetchall()

            best: dict[str, float] = {}
            for _company_id, alias, ticker, sim, exact in rows:
                if alias.strip().lower() in GENERIC:
                    continue
                if alias.upper().startswith("RU000") and len(alias) == 12:
                    continue  # ISIN-алиасы дают ложные привязки
                if not exact and float(sim) < min_similarity:
                    continue
                confidence = 0.95 if exact else min(0.9, float(sim))
                if ticker not in best or confidence > best[ticker]:
                    best[ticker] = confidence

            for ticker, confidence in best.items():
                conn.execute(
                    """
                    INSERT INTO news_tickers (article_id, ticker, confidence, method)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (article_id, ticker) DO UPDATE SET
                        confidence = GREATEST(
                            news_tickers.confidence, EXCLUDED.confidence
                        )
                    """,
                    (
                        article_id,
                        ticker,
                        confidence,
                        "exact" if confidence >= 0.95 else "trgm",
                    ),
                )
                linked += 1
            conn.execute(
                "UPDATE news_articles SET status = %s WHERE id = %s",
                ("linked" if best else "skipped", article_id),
            )
        conn.commit()
    return linked


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(link_pending())
