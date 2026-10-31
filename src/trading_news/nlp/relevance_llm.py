"""LLM relevance gate: is a linked ticker the actual subject of the article?

This is intentionally separate from event_type classification.  The model gets
an article plus the linked ticker candidates and returns only those tickers that
are the primary subject.  Broad market digests, tables and portfolio roundups
should return an empty list.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from pathlib import Path

from ..db import pool

logger = logging.getLogger(__name__)
DEFAULT_MODEL = "qwen2.5-7b-instruct-4bit-v2"
TECHNICAL_TITLE = re.compile(
    r"REPO risk parameters|мониторинг максимальных процентных ставок|"
    r"мониторинг процентных ставок|изменение параметров риска",
    re.IGNORECASE,
)


def _clean(text: str, limit: int) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def mark_technical() -> int:
    """Mark exchange/technical notices as irrelevant without an LLM call."""
    with pool().connection() as conn:
        cursor = conn.execute(
            """
            UPDATE news_tickers t
               SET relevance = 0, relevance_model = 'rule:technical'
              FROM news_articles a
             WHERE a.id = t.article_id
               AND (t.relevance IS NULL OR t.relevance <> 0)
               AND a.title ~* %s
            """,
            (r"REPO risk parameters|мониторинг максимальных процентных ставок|"
             r"мониторинг процентных ставок|изменение параметров риска",),
        )
        conn.commit()
        return cursor.rowcount


def export_pending(output: str, limit: int = 1000, days: int = 0) -> int:
    with pool().connection() as conn:
        rows = conn.execute(
            """
            WITH counts AS (
                SELECT article_id, count(*) AS n
                FROM news_tickers
                GROUP BY article_id
            )
            SELECT a.id, a.title, COALESCE(a.summary, ''), a.published_at,
                   json_agg(json_build_object(
                       'ticker', t.ticker,
                       'company', COALESCE(c.short_name, c.name, ''),
                       'confidence', t.confidence
                   ) ORDER BY t.confidence DESC, t.ticker) AS tickers
            FROM news_articles a
            JOIN news_analysis n ON n.article_id = a.id
            JOIN news_sources src ON src.id = a.source_id AND src.enabled
            JOIN counts cnt ON cnt.article_id = a.id
            JOIN news_tickers t ON t.article_id = a.id
            LEFT JOIN instruments i ON i.ticker = t.ticker
            LEFT JOIN companies c ON c.id = i.company_id
            WHERE cnt.n <= 10
              AND (%s = 0 OR a.published_at >= now() - (%s || ' days')::interval)
              AND EXISTS (
                  SELECT 1 FROM news_tickers t2
                  WHERE t2.article_id = a.id AND t2.relevance IS NULL
              )
            GROUP BY a.id, a.title, a.summary, a.published_at
            ORDER BY a.published_at DESC
            LIMIT %s
            """,
            (days, days, limit),
        ).fetchall()
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for article_id, title, summary, published_at, tickers in rows:
            handle.write(json.dumps({
                "id": article_id,
                "title": title,
                "summary": _clean(summary, 1600),
                "published_at": published_at.isoformat() if published_at else None,
                "tickers": tickers,
            }, ensure_ascii=False) + "\n")
    return len(rows)


def _parse(text: str, candidates: set[str]) -> list[str] | None:
    text = (text or "").strip()
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            payload = json.loads(text[start : end + 1])
            values = payload.get("scores") if isinstance(payload, dict) else None
        except json.JSONDecodeError:
            values = None
    else:
        # Qwen often returns a bare [] for "nothing relevant".
        try:
            values = json.loads(text) if text.startswith("[") else None
        except json.JSONDecodeError:
            values = None
    if not isinstance(values, dict):
        return None
    out = {}
    for key, value in values.items():
        ticker = str(key).upper()
        if ticker not in candidates:
            continue
        try:
            score = float(value)
        except (TypeError, ValueError):
            continue
        out[ticker] = max(0.0, min(1.0, score))
    return out


def apply_predictions(path: str, model: str = DEFAULT_MODEL) -> int:
    updated = 0
    with pool().connection() as conn:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            article_id = int(row.get("id") or 0)
            if not article_id:
                continue
            candidates = {
                str(row_[0]).upper()
                for row_ in conn.execute(
                    "SELECT ticker FROM news_tickers WHERE article_id = %s",
                    (article_id,),
                ).fetchall()
            }
            relevant = _parse(json.dumps(row), candidates)
            if relevant is None:
                if row.get("ok") is False:
                    # Mark failed rows as not relevant instead of retrying forever.
                    cursor = conn.execute(
                        "UPDATE news_tickers SET relevance = 0, relevance_model = %s "
                        "WHERE article_id = %s AND relevance IS NULL",
                        ("llm:failed", article_id),
                    )
                    updated += cursor.rowcount
                continue
            cursor = conn.execute(
                "UPDATE news_tickers SET relevance = 0, relevance_model = %s "
                "WHERE article_id = %s",
                (f"llm:{model}", article_id),
            )
            updated += cursor.rowcount
            for ticker, score in relevant.items():
                cursor = conn.execute(
                    "UPDATE news_tickers SET relevance = %s, relevance_model = %s "
                    "WHERE article_id = %s AND ticker = %s",
                    (score, f"llm:{model}", article_id, ticker),
                )
                updated += cursor.rowcount
            updated += cursor.rowcount
        conn.commit()
    return updated


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="LLM relevance gate")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("mark-technical")
    export = sub.add_parser("export")
    export.add_argument("--output", required=True)
    export.add_argument("--limit", type=int, default=1000)
    export.add_argument("--days", type=int, default=0)
    apply = sub.add_parser("apply")
    apply.add_argument("--predictions", required=True)
    apply.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args()
    if args.command == "mark-technical":
        print({"marked": mark_technical()})
    elif args.command == "export":
        print({"exported": export_pending(args.output, args.limit, args.days)})
    else:
        print({"updated": apply_predictions(args.predictions, args.model)})


if __name__ == "__main__":
    main()
