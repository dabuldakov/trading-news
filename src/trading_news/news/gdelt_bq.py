"""GDELT 2.0 GKG через BigQuery: исторические URL для news pipeline.

GDELT отдаёт URL, дату, tone и themes, но не заголовок. Поэтому экспортёр
складывает документы в staging ``gdelt_documents``, а ``--enrich`` добирает
заголовок/лид со страницы (тот же парсер, что у архивного скрапера).

Требуется Google Cloud SDK и доступ к public dataset ``gdelt-bq``:
    pip install -e ".[bigquery]"
    gcloud auth application-default login
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

from psycopg.types.json import Jsonb

from ..db import pool
from .archive import ArchiveSource, _fetch, _source_id, parse_article
from .ingest import article_hash

logger = logging.getLogger(__name__)

DEFAULT_DOMAINS = (
    "rbc.ru",
    "kommersant.ru",
    "vedomosti.ru",
    "tass.ru",
    "interfax.ru",
    "1prime.ru",
    "banki.ru",
    "lenta.ru",
    "smart-lab.ru",
    "finam.ru",
    "bcs-express.ru",
)

DOMAIN_SOURCES: dict[str, tuple[str, float]] = {
    "rbc.ru": ("rbc", 0.7),
    "kommersant.ru": ("kommersant", 0.8),
    "vedomosti.ru": ("vedomosti", 0.8),
    "tass.ru": ("tass", 0.8),
    "interfax.ru": ("interfax", 0.85),
    "1prime.ru": ("prime", 0.6),
    "banki.ru": ("banki", 0.6),
    "lenta.ru": ("lenta", 0.6),
    "smart-lab.ru": ("smartlab", 0.4),
    "finam.ru": ("finam", 0.5),
    "bcs-express.ru": ("bcsexpress", 0.6),
}


def _clean_domain(value: str) -> str:
    domain = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9.-]+", domain):
        raise ValueError(f"bad domain {value!r}")
    return domain


def build_query(start: str, end: str, domains: list[str] | tuple[str, ...]) -> str:
    clean = ", ".join(f"'{_clean_domain(item)}'" for item in domains)
    return f"""
SELECT DATE, SourceCommonName, DocumentIdentifier, V2Tone, Themes
FROM `gdelt-bq.gdeltv2.gkg_partitioned`
WHERE _PARTITIONDATE BETWEEN DATE '{start}' AND DATE '{end}'
  AND SourceCommonName IN ({clean})
  AND DocumentIdentifier LIKE 'http%'
""".strip()


def parse_row(row: dict) -> dict | None:
    url = str(row.get("DocumentIdentifier") or "").strip()
    domain = str(row.get("SourceCommonName") or "").strip().lower()
    raw_date = str(row.get("DATE") or "").strip()
    if not url or not domain or len(raw_date) < 8:
        return None
    try:
        if len(raw_date) >= 14:
            published = datetime.strptime(raw_date[:14], "%Y%m%d%H%M%S")
        else:
            published = datetime.strptime(raw_date[:8], "%Y%m%d")
    except ValueError:
        return None
    tone = None
    tone_raw = str(row.get("V2Tone") or "").split(",", 1)[0]
    try:
        tone = float(tone_raw)
    except ValueError:
        pass
    return {
        "url": url,
        "domain": domain,
        "published_at": published.replace(tzinfo=UTC).isoformat(),
        "tone": tone,
        "themes": str(row.get("Themes") or ""),
    }


def export(
    start: str,
    end: str,
    *,
    domains: list[str] | tuple[str, ...] = DEFAULT_DOMAINS,
    out: str = "data/gdelt.jsonl",
    project: str | None = None,
    limit: int = 0,
) -> int:
    try:
        from google.cloud import bigquery
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError(
            "нужен google-cloud-bigquery: pip install -e '.[bigquery]'"
        ) from exc
    query = build_query(start, end, domains)
    if limit:
        query += f"\nLIMIT {int(limit)}"
    client = bigquery.Client(project=project)
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with path.open("w", encoding="utf-8") as handle:
        for row in client.query(query).result():
            item = parse_row(dict(row))
            if item is None:
                continue
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
            written += 1
    logger.info("gdelt: %s rows → %s", written, path)
    return written


def load_jsonl(path: str) -> int:
    written = 0
    with Path(path).open(encoding="utf-8") as handle, pool().connection() as conn:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            item = json.loads(line)
            row = conn.execute(
                """
                INSERT INTO gdelt_documents
                    (url, published_at, domain, tone, themes)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (url) DO NOTHING
                RETURNING url
                """,
                (
                    item["url"],
                    item["published_at"],
                    item["domain"],
                    item.get("tone"),
                    item.get("themes"),
                ),
            ).fetchone()
            if row:
                written += 1
        conn.commit()
    return written


def _domain_source(domain: str) -> tuple[str, float]:
    for key, value in DOMAIN_SOURCES.items():
        if domain == key or domain.endswith("." + key):
            return value
    return domain.replace(".", "_"), 0.5


def enrich(limit: int = 200) -> dict:
    with pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT url, domain
            FROM gdelt_documents
            WHERE resolved = false
            ORDER BY published_at DESC
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
    inserted = 0
    failed = 0
    for url, domain in rows:
        try:
            item = parse_article(_fetch(url), url)
        except Exception as exc:  # noqa: BLE001 - один URL не рвёт цикл
            logger.debug("gdelt enrich %s: %s", url, exc)
            item = None
        if item is None:
            failed += 1
            with pool().connection() as conn:
                conn.execute(
                    "UPDATE gdelt_documents SET resolved = true WHERE url = %s",
                    (url,),
                )
                conn.commit()
            continue
        name, credibility = _domain_source(domain)
        published = item["published"] or datetime.now(UTC)
        with pool().connection() as conn:
            source = ArchiveSource(
                name=name, sitemaps=(), credibility=credibility
            )
            source_id = _source_id(conn, source)
            row = conn.execute(
                """
                INSERT INTO news_articles
                    (source_id, url, title, summary, published_at,
                     lang, hash, raw)
                VALUES (%s, %s, %s, %s, %s, 'ru', %s, %s)
                ON CONFLICT (hash) DO NOTHING
                RETURNING id
                """,
                (
                    source_id,
                    url,
                    item["title"],
                    item["summary"],
                    published,
                    article_hash(item["title"], url),
                    Jsonb({"gdelt": True, "domain": domain}),
                ),
            ).fetchone()
            conn.execute(
                "UPDATE gdelt_documents SET resolved = true WHERE url = %s",
                (url,),
            )
            conn.commit()
            if row:
                inserted += 1
    return {"requested": len(rows), "inserted": inserted, "failed": failed}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="GDELT BigQuery exporter")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--domains", default=",".join(DEFAULT_DOMAINS))
    parser.add_argument("--out", default="data/gdelt.jsonl")
    parser.add_argument("--project")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--sql", action="store_true")
    parser.add_argument("--load")
    parser.add_argument("--enrich", type=int, default=0)
    args = parser.parse_args()
    domains = [part.strip() for part in args.domains.split(",") if part.strip()]
    if args.sql:
        print(build_query(args.start, args.end, domains))
    elif args.load:
        print({"loaded": load_jsonl(args.load)})
    elif args.enrich:
        print(enrich(args.enrich))
    else:
        print(
            {
                "exported": export(
                    args.start,
                    args.end,
                    domains=domains,
                    out=args.out,
                    project=args.project,
                    limit=args.limit,
                )
            }
        )
