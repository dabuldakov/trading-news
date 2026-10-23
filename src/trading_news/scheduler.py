from __future__ import annotations

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .config import settings
from .news.ingest import ingest_all
from .news.telegram import ingest_telegram
from .nlp.classify import classify_pending
from .reference.sync import sync_reference


def build_scheduler() -> BlockingScheduler:
    scheduler = BlockingScheduler(timezone=settings.tz)
    scheduler.add_job(
        sync_reference,
        CronTrigger(
            hour=settings.reference_cron_hour,
            minute=settings.reference_cron_minute,
            timezone=settings.tz,
        ),
        id="reference",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        ingest_all,
        IntervalTrigger(seconds=settings.news_interval_sec, timezone=settings.tz),
        id="news",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        ingest_telegram,
        IntervalTrigger(minutes=10, timezone=settings.tz),
        id="telegram",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        classify_pending,
        IntervalTrigger(minutes=5, timezone=settings.tz),
        id="nlp",
        max_instances=1,
        coalesce=True,
    )
    return scheduler
