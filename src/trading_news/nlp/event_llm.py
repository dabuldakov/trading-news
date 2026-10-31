"""Event-type only LLM pipeline.

LLM предсказывает только ``event_type``; sentiment/materiality остаются от
rules-v2. Модель работает вне прод-процесса (DataSphere/API), а этот модуль
экспортирует очередь статей и применяет готовые предсказания к news_analysis.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from pathlib import Path

from ..db import pool
from .classify import EVENTS

logger = logging.getLogger(__name__)

EVENT_TYPES = [name for name, _ in EVENTS] + ["other"]
DEFAULT_MODEL = "qwen2.5-14b-instruct-4bit-v2"
PROMPT = (
    "Ты классификатор финансовых новостей MOEX. Определи event_type.\n"
    "Классы:\n"
    "- dividend: объявление, рекомендация или выплата дивидендов.\n"
    "- earnings: финансовые результаты, выручка, прибыль, прогнозы компании.\n"
    "- m&a: покупка/продажа компании или актива, слияние, SPO/IPO.\n"
    "- sanctions: санкции, ограничения, запреты, блокировки со стороны стран, ЕС, США; санкционные иски.\n"
    "- rate: ключевая ставка ЦБ, ставки ФРС/ЕЦБ, денежно-кредитная политика.\n"
    "- cpi: инфляция, потребительские цены, инфляционные ожидания.\n"
    "- gdp: ВВП, экономический рост, промышленное производство страны.\n"
    "- budget: федеральный/региональный бюджет, налоги, госрасходы, НДС.\n"
    "- regulation: решения регуляторов (ФАС, ЦБ, тарифы, лицензии, законы), НЕ санкции.\n"
    "- contract: контракты, поставки, соглашения, тендеры.\n"
    "- management: назначения, отставки, совет директоров, корпоративное управление.\n"
    "- production: добыча, выпуск, мощности, ремонты, аварии.\n"
    "- rating: кредитные рейтинги и их изменения.\n"
    "- other: нет подходящего класса, макро без России или нерелевантная новость.\n"
    "Правила: санкции/ограничения -> sanctions, не regulation. Назначение/отставка -> management. "
    "Бюджет/налоги -> budget. Не российская новость -> other.\n"
    'Верни строго JSON без пояснений: {"event_type": "..."}\n'
)


def parse_event(text: str) -> str | None:
    text = (text or "").strip()
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    event_type = str(payload.get("event_type") or "other")
    return event_type if event_type in EVENT_TYPES else "other"


OFFICIAL_TELEGRAM = {
    "telegram:minpromtorg_ru",
    "telegram:rostecru",
    "telegram:mash_tech",
    "telegram:eprussia",
    "telegram:government_rus",
    "telegram:minec_russia",
    "telegram:MoscowEcon",
    "telegram:minfin",
    "telegram:centralbank_russia",
}


def export_pending(
    output: str,
    limit: int = 500,
    days: int = 0,
    reactions_only: bool = False,
    linked_only: bool = False,
    exclude_official: bool = False,
) -> int:
    """Выгрузить статьи без LLM-разметки event_type для внешнего прогона.

    ``linked_only`` отбрасывает broad-новости без тикера/reactions.
    ``exclude_official`` убирает официальные Telegram-каналы, которые в основном
    дают PR/дайджесты; их можно размечать отдельным батчем.
    """
    official = sorted(OFFICIAL_TELEGRAM) if exclude_official else []
    with pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT a.id, a.title, COALESCE(a.summary, ''), a.published_at
            FROM news_articles a
            JOIN news_analysis n ON n.article_id = a.id
            JOIN news_sources s ON s.id = a.source_id
            WHERE n.event_model IS NULL
              AND (%s = 0 OR a.published_at >= now() - (%s || ' days')::interval)
              AND (NOT %s OR EXISTS (
                  SELECT 1 FROM news_reactions r WHERE r.article_id = a.id
              ))
              AND (NOT %s OR EXISTS (
                  SELECT 1 FROM news_tickers t WHERE t.article_id = a.id
              ) OR EXISTS (
                  SELECT 1 FROM news_reactions r2 WHERE r2.article_id = a.id
              ))
              AND (NOT %s OR s.name <> ALL(%s))
            ORDER BY a.published_at DESC
            LIMIT %s
            """,
            (
                days,
                days,
                reactions_only,
                linked_only,
                exclude_official,
                official,
                limit,
            ),
        ).fetchall()
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for article_id, title, summary, published_at in rows:
            handle.write(
                json.dumps(
                    {
                        "id": article_id,
                        "title": title,
                        "summary": summary,
                        "published_at": published_at.isoformat()
                        if published_at
                        else None,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    return len(rows)


def apply_predictions(path: str, model: str = DEFAULT_MODEL) -> int:
    """Применить JSONL ``{id, event_type}``; sentiment/materiality не трогаем."""
    updated = 0
    with pool().connection() as conn:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("ok") is False:
                continue
            event_type = parse_event(json.dumps(row)) or "other"
            article_id = int(row["id"])
            cursor = conn.execute(
                """
                UPDATE news_analysis
                SET event_type = %s, event_model = %s
                WHERE article_id = %s
                """,
                (event_type, f"llm:{model}", article_id),
            )
            updated += cursor.rowcount
        conn.commit()
    return updated


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="LLM event_type pipeline")
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export")
    export.add_argument("--output", required=True)
    export.add_argument("--limit", type=int, default=500)
    export.add_argument("--days", type=int, default=0)
    export.add_argument("--reactions-only", action="store_true")
    export.add_argument("--linked-only", action="store_true")
    export.add_argument("--exclude-official", action="store_true")
    apply = sub.add_parser("apply")
    apply.add_argument("--predictions", required=True)
    apply.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args()
    if args.command == "export":
        print(
            {
                "exported": export_pending(
                    args.output, args.limit, args.days, args.reactions_only
                )
            }
        )
    else:
        print({"updated": apply_predictions(args.predictions, args.model)})


if __name__ == "__main__":
    main()
