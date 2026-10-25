"""Архивный скрапер RBC / Kommersant / Vedomosti по sitemap-файлам.

Сайтмапы отдают URL + lastmod; заголовок/лид берём со страницы статьи.
Для истории это дешевле Wayback: не зависим от наличия снимков RSS.
"""

from __future__ import annotations

import argparse
import logging
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from psycopg.types.json import Jsonb

from ..db import pool
from .ingest import article_hash

logger = logging.getLogger(__name__)

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
TIMEOUT = 25.0


@dataclass(frozen=True)
class ArchiveSource:
    name: str
    sitemaps: tuple[str, ...]
    credibility: float
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    max_per_sitemap: int = 0
    max_sitemaps: int = 0
    monthly: bool = False


SOURCES: dict[str, ArchiveSource] = {
    "vedomosti": ArchiveSource(
        name="vedomosti",
        sitemaps=("https://www.vedomosti.ru/sitemap.xml",),
        credibility=0.8,
        include=(
            "/business/",
            "/economics/",
            "/finance/",
            "/companies/",
            "/markets/",
            "/analytics/",
            "/industry/",
            "/capital/",
            "/retail/",
        ),
        max_per_sitemap=600,
    ),
    "rbc": ArchiveSource(
        name="rbc",
        sitemaps=("https://www.rbc.ru/sitemap_index.xml",),
        credibility=0.7,
        include=(
            "/business/",
            "/economics/",
            "/finance/",
            "/companies/",
            "/rbcfreenews/",
        ),
        max_per_sitemap=400,
        max_sitemaps=400,
    ),
    "kommersant": ArchiveSource(
        name="kommersant",
        sitemaps=("https://www.kommersant.ru/sitemaps/all_docs_sitemap.xml",),
        credibility=0.8,
        include=("/doc/",),
        max_per_sitemap=250,
        monthly=True,
    ),
}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_sitemap(raw: bytes) -> tuple[str, list[dict]]:
    root = ET.fromstring(raw)
    kind = _local(root.tag)
    entries = []
    for child in root:
        if _local(child.tag) not in ("sitemap", "url"):
            continue
        loc = lastmod = ""
        for item in child:
            name = _local(item.tag)
            if name == "loc":
                loc = (item.text or "").strip()
            elif name == "lastmod":
                lastmod = (item.text or "").strip()
        if loc:
            entries.append({"loc": loc, "lastmod": lastmod})
    return kind, entries


def _parse_time(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _meta(soup: BeautifulSoup, *, prop: str = "", name: str = "") -> str:
    if prop:
        tag = soup.find("meta", attrs={"property": prop})
        if tag and tag.get("content"):
            return str(tag["content"]).strip()
    if name:
        tag = soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return str(tag["content"]).strip()
    return ""


def parse_article(raw: bytes, url: str) -> dict | None:
    soup = BeautifulSoup(raw, "html.parser")
    title = _meta(soup, prop="og:title") or _meta(soup, name="twitter:title")
    if not title and soup.title and soup.title.string:
        title = soup.title.string.strip()
    if not title:
        return None
    title = re.sub(r"\s+", " ", title)
    for suffix in (" — РБК", " - Ведомости", " - Коммерсантъ"):
        if title.endswith(suffix):
            title = title[: -len(suffix)].strip()
    summary = _meta(soup, name="description") or _meta(
        soup, prop="og:description"
    )
    published = (
        _meta(soup, prop="article:published_time")
        or _meta(soup, name="date")
        or _meta(soup, prop="og:updated_time")
    )
    return {
        "title": title,
        "summary": summary,
        "published": _parse_time(published),
        "url": url,
    }


def _fetch(url: str) -> bytes:
    with httpx.Client(
        timeout=TIMEOUT, follow_redirects=True, headers={"User-Agent": UA}
    ) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.content


def _source_id(conn, source: ArchiveSource) -> int:
    row = conn.execute(
        """
        INSERT INTO news_sources (name, kind, credibility)
        VALUES (%s, 'archive', %s)
        ON CONFLICT (name) DO UPDATE SET
            kind = EXCLUDED.kind,
            credibility = GREATEST(news_sources.credibility, EXCLUDED.credibility)
        RETURNING id
        """,
        (source.name, source.credibility),
    ).fetchone()
    return int(row[0])


def _months(start: date, end: date) -> set[str]:
    out = set()
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        out.add(cursor.strftime("%Y-%m"))
        cursor = (
            date(cursor.year + 1, 1, 1)
            if cursor.month == 12
            else date(cursor.year, cursor.month + 1, 1)
        )
    return out


def _match(url: str, source: ArchiveSource) -> bool:
    path = urlparse(url).path
    if source.include and not any(part in path for part in source.include):
        return False
    return not any(part in path for part in source.exclude)


def scrape(
    site: str,
    start: str,
    end: str,
    *,
    limit: int = 500,
    workers: int = 4,
    pause: float = 0.1,
) -> dict:
    source = SOURCES[site]
    start_dt = datetime.fromisoformat(start).replace(tzinfo=UTC)
    end_dt = datetime.fromisoformat(end).replace(tzinfo=UTC) + timedelta(days=1)
    wanted_months = _months(start_dt.date(), end_dt.date())

    sitemap_urls: list[str] = []
    for index in source.sitemaps:
        try:
            kind, entries = parse_sitemap(_fetch(index))
        except Exception as exc:  # noqa: BLE001
            logger.warning("archive %s sitemap %s: %s", site, index, exc)
            continue
        if kind == "sitemapindex":
            for entry in entries:
                loc = entry["loc"]
                if source.monthly:
                    match = re.search(r"sitemap-(\d{4}-\d{2})\.xml$", loc)
                    if not match or match.group(1) not in wanted_months:
                        continue
                sitemap_urls.append(loc)
        else:
            sitemap_urls.append(index)
    if source.max_sitemaps:
        sitemap_urls = sitemap_urls[: source.max_sitemaps]

    candidates: list[tuple[str, datetime]] = []
    for sitemap in sitemap_urls:
        try:
            kind, entries = parse_sitemap(_fetch(sitemap))
        except Exception as exc:  # noqa: BLE001
            logger.warning("archive %s %s: %s", site, sitemap, exc)
            continue
        if kind != "urlset":
            continue
        month_match = re.search(r"sitemap-(\d{4})-(\d{2})\.xml$", sitemap)
        month_fallback = None
        if source.monthly and month_match:
            month_fallback = datetime(
                int(month_match.group(1)),
                int(month_match.group(2)),
                1,
                tzinfo=UTC,
            )
        found = []
        for entry in entries:
            url = entry["loc"]
            if not _match(url, source):
                continue
            if month_fallback is not None:
                found.append((url, month_fallback))
                continue
            when = _parse_time(entry["lastmod"])
            if when is None or when < start_dt or when >= end_dt:
                continue
            found.append((url, when))
        if source.max_per_sitemap and len(found) > source.max_per_sitemap:
            step = max(1, len(found) // source.max_per_sitemap)
            found = found[::step][: source.max_per_sitemap]
        candidates.extend(found)
        if len(candidates) >= limit * 3:
            break

    seen: set[str] = set()
    unique = []
    for url, when in candidates:
        if url in seen:
            continue
        seen.add(url)
        unique.append((url, when))
    unique = unique[:limit]

    inserted = 0
    failed = 0
    fetched = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(_fetch, url): (url, when)
            for url, when in unique
        }
        for future in as_completed(futures):
            url, fallback = futures[future]
            fetched += 1
            try:
                item = parse_article(future.result(), url)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                logger.debug("archive %s article %s: %s", site, url, exc)
                item = None
            if item is None:
                failed += 1
                continue
            published = item["published"] or fallback
            if published < start_dt or published >= end_dt:
                continue
            with pool().connection() as conn:
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
                        Jsonb({"archive": site}),
                    ),
                ).fetchone()
                conn.commit()
                if row:
                    inserted += 1
            if pause:
                time.sleep(pause)
    return {
        "site": site,
        "sitemaps": len(sitemap_urls),
        "candidates": len(candidates),
        "fetched": fetched,
        "inserted": inserted,
        "failed": failed,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser(description="Archive scraper")
    parser.add_argument("--site", choices=sorted(SOURCES), required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--pause", type=float, default=0.1)
    args = parser.parse_args()
    print(
        scrape(
            args.site,
            args.start,
            args.end,
            limit=args.limit,
            workers=args.workers,
            pause=args.pause,
        )
    )
