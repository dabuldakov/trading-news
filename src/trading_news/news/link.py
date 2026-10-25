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



SECTOR_KEYWORDS = {
    "oil": {"oil", "brent", "wti", "crude", "opec", "нефт"},
    "gas": {"gas", "lng", "газ"},
    "gold": {"gold", "золот"},
    "metals": {
        "copper", "nickel", "aluminium", "aluminum", "steel",
        "мед", "никел", "стал",
    },
    "china": {"china", "chinese", "китай"},
    "rates": {"fed", "fomc", "ecb", "rate hike", "rate cut", "ставк"},
}

SECTOR_TICKERS = {
    "oil": ["GAZP", "ROSN", "LKOH", "NVTK", "TATN", "SNGS", "SNGSP"],
    "gas": ["GAZP", "NVTK"],
    "gold": ["PLZL", "GMKN"],
    "metals": ["GMKN", "NLMK", "MAGN", "CHMF", "RUAL", "ALRS"],
    "china": ["RUAL", "PLZL", "GMKN", "MOEX"],
    "rates": ["SBER", "VTBR", "MOEX"],
}


def active_tickers(conn) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT ticker FROM instruments WHERE status = 'active'"
        ).fetchall()
    }


def link_external(
    conn,
    article_id: int,
    text: str,
    active: set[str] | None = None,
) -> int:
    lowered = (text or "").lower()
    if active is None:
        active = active_tickers(conn)
    linked = 0
    for sector, words in SECTOR_KEYWORDS.items():
        if not any(word in lowered for word in words):
            continue
        for ticker in SECTOR_TICKERS.get(sector, []):
            if ticker not in active:
                continue
            conn.execute(
                """
                INSERT INTO news_tickers (article_id, ticker, confidence, method)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (article_id, ticker) DO UPDATE SET
                    confidence = GREATEST(news_tickers.confidence, EXCLUDED.confidence)
                """,
                (article_id, ticker, 0.5, f"sector:{sector}"),
            )
            linked += 1
    return linked


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
        active = active_tickers(conn)
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
                  AND (
                    (%s ILIKE '%%' || a.alias || '%%')
                    OR similarity(lower(a.alias), lower(%s)) >= %s
                  )
                ORDER BY exact DESC, sim DESC
                LIMIT 100
                """,
                (text, text, text, text, min_similarity),
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
            sector_links = link_external(conn, article_id, text, active)
            conn.execute(
                "UPDATE news_articles SET status = %s WHERE id = %s",
                ("linked" if (best or sector_links) else "skipped", article_id),
            )
        conn.commit()
    return linked


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(link_pending())

def _norm(text: str) -> str:
    return " ".join((text or "").lower().replace("ё", "е").split())


def _alias_map(conn) -> dict[str, list[tuple[str, str]]]:
    rows = conn.execute(
        """
        SELECT a.company_id, a.alias, i.ticker
        FROM aliases a
        JOIN instruments i
          ON i.company_id = a.company_id AND i.status = 'active'
        WHERE length(a.alias) >= 4
        """
    ).fetchall()
    mapping: dict[str, list[tuple[str, str]]] = {}
    for _company_id, alias, ticker in rows:
        text = _norm(alias)
        if not text or text in GENERIC:
            continue
        if text.upper().startswith("RU000") and len(text) == 12:
            continue
        mapping.setdefault(text, [])
        if ticker not in [item[0] for item in mapping[text]]:
            mapping[text].append((ticker, "exact"))
    return mapping


def link_pending_fast(limit: int = 100000, batch: int = 500) -> dict:
    """Быстрая привязка архивных статей: точный alias-substring без pg_trgm.

    Для live остаётся :func:`link_pending` (fuzzy). Здесь 3k алиасов и десятки
    тысяч статей: проверка ``alias in text`` в памяти на порядки быстрее, чем
    similarity-запрос на каждую статью.
    """
    processed = 0
    linked = 0
    with pool().connection() as conn:
        aliases = _alias_map(conn)
        active = active_tickers(conn)
        keys = list(aliases)
        while processed < limit:
            rows = conn.execute(
                """
                SELECT id, title, COALESCE(summary, '')
                FROM news_articles
                WHERE status = 'new'
                ORDER BY published_at
                LIMIT %s
                """,
                (min(batch, limit - processed),),
            ).fetchall()
            if not rows:
                break
            for article_id, title, summary in rows:
                text = _norm(f"{title} {summary}")
                best: dict[str, str] = {}
                for alias in keys:
                    if alias in text:
                        for ticker, method in aliases[alias]:
                            best.setdefault(ticker, method)
                for ticker, method in best.items():
                    conn.execute(
                        """
                        INSERT INTO news_tickers
                            (article_id, ticker, confidence, method)
                        VALUES (%s, %s, 0.95, %s)
                        ON CONFLICT (article_id, ticker) DO UPDATE SET
                            confidence = GREATEST(
                                news_tickers.confidence, EXCLUDED.confidence
                            )
                        """,
                        (article_id, ticker, method),
                    )
                    linked += 1
                sector_links = link_external(conn, article_id, text, active)
                conn.execute(
                    "UPDATE news_articles SET status = %s WHERE id = %s",
                    ("linked" if (best or sector_links) else "skipped", article_id),
                )
                processed += 1
            conn.commit()
    return {"processed": processed, "linked": linked}
