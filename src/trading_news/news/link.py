from __future__ import annotations

import logging
import re
from functools import lru_cache

from ..db import pool

logger = logging.getLogger(__name__)

_WORD_BEFORE = r"(?<![0-9a-zа-яё_])"
_WORD_AFTER = r"(?![0-9a-zа-яё_])"
_RU_CASE = r"(?:а|я|у|ю|ом|ем|е|ы|и|ой|ей)?"
_CASE_SENSITIVE_ALIASES = {"самолет", "магнит", "полюс"}
_AVIATION_CONTEXT = {
    "рейс", "рейса", "рейсы", "борт", "борта", "аэропорт", "аэропорта",
    "вылет", "вылета", "прилет", "flight", "авиакомпан", "aviation",
}
_COMPANY_CONTEXT = {
    "гк", "пао", "акци", "выручк", "дивиденд", "застройщик", "девелопер",
    "мсфо", "отчет", "прибыл", "квартал", "жил", "новострой", "капитализац",
}


def _ambiguous_context_ok(text: str, normalized: str) -> bool:
    """Не линковать обычное слово «самолет» на авиационную новость."""
    if normalized != "самолет":
        return True
    low = _norm(text)
    aviation = any(word in low for word in _AVIATION_CONTEXT)
    company = any(word in low for word in _COMPANY_CONTEXT)
    return not aviation or company


def _norm(text: str) -> str:
    return " ".join((text or "").lower().replace("ё", "е").split())


@lru_cache(maxsize=8192)
def _alias_pattern(alias: str) -> re.Pattern[str]:
    normalized = _norm(alias)
    # Русские названия в новостях часто стоят в косвенном падеже.
    suffix = _RU_CASE if re.search(r"[а-я]$", normalized) else ""
    return re.compile(
        rf"{_WORD_BEFORE}{re.escape(normalized)}{suffix}{_WORD_AFTER}"
    )


@lru_cache(maxsize=8192)
def _ticker_pattern(alias: str) -> re.Pattern[str]:
    return re.compile(
        rf"(?<![A-Za-z0-9_]){re.escape(alias.upper())}(?![A-Za-z0-9_])"
    )


def _alias_hit(text: str, alias: str) -> bool:
    normalized = _norm(alias)
    if not normalized:
        return False
    # Короткие латинские тикеры (HEAD, DATA, CARM) совпадают только в
    # верхнем регистре: иначе «Head of...» становится HeadHunter.
    if normalized.isascii() and len(normalized) <= 5:
        return bool(_ticker_pattern(alias).search(text))
    if normalized in _CASE_SENSITIVE_ALIASES:
        proper = normalized[:1].upper() + normalized[1:]
        suffix = _RU_CASE if re.search(r"[а-я]$", normalized) else ""
        if not re.search(
            rf"{_WORD_BEFORE}{re.escape(proper)}{suffix}{_WORD_AFTER}",
            text,
        ):
            return False
        return _ambiguous_context_ok(text, normalized)
    return bool(_alias_pattern(alias).search(_norm(text)))


@lru_cache(maxsize=256)
def _keyword_pattern(word: str) -> re.Pattern[str]:
    return re.compile(rf"{_WORD_BEFORE}{re.escape(_norm(word))}{_WORD_AFTER}")


def _has_keyword(text: str, word: str) -> bool:
    return bool(_keyword_pattern(word).search(_norm(text)))


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
    "oil": {
        "oil", "brent", "wti", "crude", "opec",
        "нефть", "нефти", "нефтью", "нефтяной", "нефтяная", "нефтяные",
    },
    "gas": {"gas", "lng", "газ", "газа", "газовый", "газовая", "газовые"},
    "gold": {"gold", "золото", "золота", "золотой", "золотая"},
    "steel": {
        "steel", "сталь", "стали", "стальной", "стальная", "стальные",
        "чугун", "прокат",
    },
    "nickel": {"nickel", "никель", "никеля"},
    "copper": {"copper", "медь", "меди", "медный"},
    "aluminium": {"aluminium", "aluminum", "алюминий", "алюминия"},
    "rates": {
        "fed", "fomc", "ecb", "rate hike", "rate cut",
        "ставка", "ставки", "ставок", "ставке", "ставку", "ставкой",
    },
}

SECTOR_TICKERS = {
    "oil": ["GAZP", "ROSN", "LKOH", "NVTK", "TATN", "SNGS", "SNGSP"],
    "gas": ["GAZP", "NVTK"],
    "gold": ["PLZL"],
    "steel": ["MAGN", "NLMK", "CHMF", "TRMK"],
    "nickel": ["GMKN"],
    "copper": ["GMKN", "RUAL"],
    "aluminium": ["RUAL"],
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
    if active is None:
        active = active_tickers(conn)
    linked = 0
    for sector, words in SECTOR_KEYWORDS.items():
        if not any(_has_keyword(text, word) for word in words):
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


def aliases_for_tickers(conn, tickers: set[str]) -> dict[str, list[str]]:
    """Алиасы активных бумаг для проверки уже готовых сигналов."""
    if not tickers:
        return {}
    rows = conn.execute(
        """
        SELECT i.ticker, a.alias
        FROM aliases a
        JOIN instruments i ON i.company_id = a.company_id
        WHERE i.status = 'active' AND i.ticker = ANY(%s)
        """,
        (sorted(tickers),),
    ).fetchall()
    result: dict[str, list[str]] = {}
    for ticker, alias in rows:
        result.setdefault(ticker, []).append(alias)
    return result


def link_is_valid(
    text: str,
    ticker: str,
    method: str | None,
    aliases: list[str] | None = None,
) -> bool:
    """Проверить старую привязку актуальными правилами линковки."""
    method = str(method or "")
    if method.startswith("sector:"):
        sector = method.split(":", 1)[1]
        return ticker in SECTOR_TICKERS.get(sector, []) and any(
            _has_keyword(text, word) for word in SECTOR_KEYWORDS.get(sector, set())
        )
    return any(_alias_hit(text, alias) for alias in (aliases or []))


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
            normalized_text = _norm(text)
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
            for _company_id, alias, ticker, sim, _substring in rows:
                alias_text = _norm(alias)
                if alias_text in GENERIC:
                    continue
                if alias_text.upper().startswith("RU000") and len(alias_text) == 12:
                    continue  # ISIN-алиасы дают ложные привязки
                exact = _alias_hit(text, alias)
                if not exact:
                    # Подстрочное совпадение внутри другого слова (HEAD в Ahead,
                    # POSI в positive) — не повод для fuzzy-привязки.
                    if alias_text in normalized_text:
                        continue
                    if len(alias_text) <= 4 or (
                        alias_text.isascii() and len(alias_text) <= 5
                    ):
                        continue
                    if float(sim) < max(0.65, min_similarity):
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
                raw_text = f"{title} {summary}"
                text = _norm(raw_text)
                best: dict[str, str] = {}
                for alias in keys:
                    if alias in text and _alias_hit(raw_text, alias):
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
