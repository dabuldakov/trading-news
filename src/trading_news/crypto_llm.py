"""Crypto-specific LLM pass: event_type + asset relevance + direction.

This module is intentionally separate from MOEX relevance/direction.  It writes
only to a crypto-news database (the caller supplies DATABASE_URL/profile).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from pathlib import Path

from .db import pool

logger = logging.getLogger(__name__)
MODEL = "qwen2.5-7b-instruct-4bit-v2"
CRYPTO_EVENTS = [
    "listing",
    "delisting",
    "hack",
    "exploit",
    "sec",
    "etf",
    "regulation",
    "partnership",
    "upgrade",
    "halving",
    "funding",
    "bankruptcy",
    "lawsuit",
    "macro",
    "exchange",
    "other",
]


def _clean(text: str, limit: int = 1800) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def export_pending(output: str, limit: int = 1000, days: int = 0) -> int:
    with pool().connection() as conn:
        rows = conn.execute(
            """
            WITH cnt AS (
                SELECT article_id, count(*) AS n
                FROM news_tickers GROUP BY article_id
            )
            SELECT a.id, a.title, COALESCE(a.summary, ''), a.published_at,
                   json_agg(json_build_object(
                       'ticker', t.ticker,
                       'name', COALESCE(i.name, t.ticker)
                   ) ORDER BY t.ticker) AS tickers
            FROM news_articles a
            JOIN news_sources s ON s.id = a.source_id AND s.enabled
            JOIN cnt ON cnt.article_id = a.id AND cnt.n <= 10
            JOIN news_tickers t ON t.article_id = a.id
            LEFT JOIN instruments i ON i.ticker = t.ticker
            LEFT JOIN news_analysis n ON n.article_id = a.id
            WHERE (%s = 0 OR a.published_at >= now() - (%s || ' days')::interval)
              AND (n.event_model IS NULL OR n.event_model NOT LIKE 'llm:crypto%%')
            GROUP BY a.id, a.title, a.summary, a.published_at
            ORDER BY a.published_at DESC
            LIMIT %s
            """,
            (days, days, limit),
        ).fetchall()
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for article_id, title, summary, published_at, tickers in rows:
            fh.write(
                json.dumps(
                    {
                        "id": article_id,
                        "title": title,
                        "summary": _clean(summary),
                        "published_at": published_at.isoformat()
                        if published_at
                        else None,
                        "tickers": tickers,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    return len(rows)


def _parse(text: str, candidates: set[str]) -> dict | None:
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
    event_type = str(payload.get("event_type") or "other").lower()
    if event_type not in CRYPTO_EVENTS:
        event_type = "other"
    assets = payload.get("assets")
    if not isinstance(assets, dict):
        assets = {}
    out = {"event_type": event_type, "assets": {}}
    for key, value in assets.items():
        ticker = str(key).upper()
        if ticker not in candidates or not isinstance(value, dict):
            continue
        try:
            relevance = float(value.get("relevance", 0.0))
            direction = float(value.get("direction", 0.0))
        except (TypeError, ValueError):
            continue
        out["assets"][ticker] = {
            "relevance": max(0.0, min(1.0, relevance)),
            "direction": max(-1.0, min(1.0, direction)),
        }
    return out


def apply_predictions(path: str, model: str = MODEL) -> int:
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
                str(r[0]).upper()
                for r in conn.execute(
                    "SELECT ticker FROM news_tickers WHERE article_id = %s",
                    (article_id,),
                ).fetchall()
            }
            parsed = _parse(json.dumps(row), candidates)
            if parsed is None:
                continue
            conn.execute(
                """
                INSERT INTO news_analysis (article_id, event_type, event_model, updated_at)
                VALUES (%s, %s, %s, now())
                ON CONFLICT (article_id) DO UPDATE SET
                    event_type = EXCLUDED.event_type,
                    event_model = EXCLUDED.event_model,
                    updated_at = now()
                """,
                (article_id, parsed["event_type"], f"llm:crypto:{model}"),
            )
            for ticker, score in parsed["assets"].items():
                conn.execute(
                    """
                    UPDATE news_tickers
                       SET relevance = %s, direction = %s,
                           relevance_model = %s, direction_model = %s
                     WHERE article_id = %s AND ticker = %s
                    """,
                    (
                        score["relevance"],
                        score["direction"],
                        f"llm:crypto:{model}",
                        f"llm:crypto:{model}",
                        article_id,
                        ticker,
                    ),
                )
                updated += 1
        conn.commit()
    return updated


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="Crypto LLM event/relevance/direction")
    sub = parser.add_subparsers(dest="command", required=True)
    exp = sub.add_parser("export")
    exp.add_argument("--output", required=True)
    exp.add_argument("--limit", type=int, default=1000)
    exp.add_argument("--days", type=int, default=0)
    app = sub.add_parser("apply")
    app.add_argument("--predictions", required=True)
    app.add_argument("--model", default=MODEL)
    args = parser.parse_args()
    if args.command == "export":
        print({"exported": export_pending(args.output, args.limit, args.days)})
    else:
        print({"updated": apply_predictions(args.predictions, args.model)})


if __name__ == "__main__":
    main()
