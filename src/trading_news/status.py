"""Operational snapshot for the news monitor."""
from __future__ import annotations

from datetime import UTC, datetime

from .config import settings
from .db import pool


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def mark_stage(
    stage: str,
    status: str = "ok",
    detail: str = "",
    *,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
) -> None:
    """Record the latest run of one pipeline stage."""
    now = finished_at or datetime.now(UTC)
    try:
        with pool().connection() as conn:
            conn.execute(
                """
                INSERT INTO news_status
                    (profile, stage, status, detail, started_at, finished_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (profile, stage) DO UPDATE SET
                    status = EXCLUDED.status,
                    detail = EXCLUDED.detail,
                    started_at = COALESCE(EXCLUDED.started_at, news_status.started_at),
                    finished_at = EXCLUDED.finished_at,
                    updated_at = now()
                """,
                (
                    getattr(settings, "news_profile", "ru"),
                    stage,
                    status,
                    detail[:1000],
                    started_at,
                    now,
                ),
            )
            conn.commit()
    except Exception:
        # Monitoring must never break the pipeline.
        pass


def snapshot() -> dict:
    profile = getattr(settings, "news_profile", "ru")
    with pool().connection() as conn:
        row = conn.execute(
            """
            SELECT
              (SELECT count(*) FROM news_articles) AS articles,
              (SELECT count(DISTINCT article_id) FROM news_tickers) AS linked,
              (SELECT count(*) FROM news_analysis WHERE event_model IS NOT NULL) AS llm,
              (SELECT count(*) FROM news_reactions) AS reactions,
              (SELECT count(*) FROM event_markers) AS markers,
              (SELECT count(*) FROM signals) AS signals,
              (SELECT count(*) FROM signals WHERE validated) AS validated,
              (SELECT count(*) FROM news_articles
                 WHERE published_at >= date_trunc('day', now())) AS today_published,
              (SELECT count(DISTINCT n.article_id) FROM news_analysis n
                 WHERE n.updated_at >= date_trunc('day', now())
                   AND n.event_model IS NOT NULL) AS today_processed,
              (SELECT count(DISTINCT a.id) FROM news_articles a
                 JOIN news_tickers t ON t.article_id = a.id
                 LEFT JOIN news_analysis n ON n.article_id = a.id
                 WHERE n.event_model IS NULL) AS pending_llm,
              (SELECT count(DISTINCT a.id) FROM news_articles a
                 JOIN news_tickers t ON t.article_id = a.id
                 LEFT JOIN news_reactions r
                   ON r.article_id = a.id AND r.ticker = t.ticker
                 WHERE t.relevance >= 1.0 AND r.article_id IS NULL) AS pending_reactions,
              (SELECT count(*) FROM news_sources WHERE enabled) AS sources_enabled,
              (SELECT count(DISTINCT s.id) FROM news_sources s
                 WHERE s.enabled AND EXISTS (
                   SELECT 1 FROM news_articles a
                   WHERE a.source_id = s.id
                     AND a.fetched_at >= now() - interval '24 hours'
                 )) AS sources_active,
              (SELECT max(fetched_at) FROM news_articles) AS last_ingest,
              (SELECT max(updated_at) FROM news_analysis
                 WHERE event_model IS NOT NULL) AS last_llm,
              (SELECT max(updated_at) FROM event_markers) AS last_markers,
              (SELECT max(published_at) FROM news_articles) AS latest_article
            """
        ).fetchone()
        stages = {
            stage: {
                "status": status,
                "detail": detail,
                "finished_at": _iso(finished),
            }
            for stage, status, detail, finished in conn.execute(
                """
                SELECT stage, status, detail, finished_at
                FROM news_status WHERE profile = %s
                """,
                (profile,),
            ).fetchall()
        }
        watermark = conn.execute(
            "SELECT max(updated_at) FROM signal_watermark"
        ).fetchone()[0]
        daily = [
            {
                "day": str(day),
                "articles": int(articles),
                "processed": int(processed),
            }
            for day, articles, processed in conn.execute(
                """
                WITH days AS (
                  SELECT generate_series(
                    date_trunc('day', now()) - interval '6 days',
                    date_trunc('day', now()),
                    interval '1 day'
                  ) AS day
                )
                SELECT d.day::date,
                       count(a.id),
                       count(n.article_id)
                FROM days d
                LEFT JOIN news_articles a
                  ON a.fetched_at >= d.day AND a.fetched_at < d.day + interval '1 day'
                LEFT JOIN news_analysis n
                  ON n.article_id = a.id
                 AND n.updated_at >= d.day
                 AND n.updated_at < d.day + interval '1 day'
                 AND n.event_model IS NOT NULL
                GROUP BY d.day
                ORDER BY d.day
                """
            ).fetchall()
        ]

    (
        articles, linked, llm, reactions, markers, signals, validated,
        today_published, today_processed, pending_llm, pending_reactions,
        sources_enabled, sources_active, last_ingest, last_llm,
        last_markers, latest_article,
    ) = row

    last_signals = stages.get("signals", {}).get("finished_at") or _iso(watermark)
    last_ingest_marker = stages.get("ingest", {}).get("finished_at") or _iso(last_ingest)
    errors = [
        {"stage": stage, "detail": item.get("detail") or ""}
        for stage, item in stages.items()
        if item.get("status") not in (None, "ok")
    ]
    stale_hours = 2 if profile == "crypto" else 4
    stale = False
    if last_ingest is not None:
        age = (datetime.now(UTC) - last_ingest).total_seconds() / 3600
        stale = age > stale_hours
    health = "error" if errors else ("warn" if stale else "ok")
    return {
        "profile": profile,
        "generated_at": datetime.now(UTC).isoformat(),
        "health": health,
        "today": {
            "published": int(today_published),
            "processed": int(today_processed),
        },
        "history": {
            "articles": int(articles),
            "linked": int(linked),
            "llm": int(llm),
            "reactions": int(reactions),
            "markers": int(markers),
            "signals": int(signals),
            "validated": int(validated),
        },
        "pending": {
            "llm": int(pending_llm),
            "reactions": int(pending_reactions),
        },
        "sources": {
            "enabled": int(sources_enabled),
            "active_24h": int(sources_active),
        },
        "pipeline": {
            "last_ingest": last_ingest_marker,
            "last_llm": _iso(last_llm),
            "last_reactions": stages.get("reactions", {}).get("finished_at"),
            "last_markers": stages.get("markers", {}).get("finished_at") or _iso(last_markers),
            "last_signals": last_signals,
            "latest_article": _iso(latest_article),
            "stages": stages,
            "errors": errors,
        },
        "daily": daily,
    }
