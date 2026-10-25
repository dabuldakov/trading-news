from __future__ import annotations

from datetime import date, timedelta

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .config import settings
from .news.ingest import ingest_all
from .news.telegram import ingest_telegram
from .nlp.classify import classify_pending
from .reference.sync import sync_reference
from .research.sector_flow import build_sector_returns
from .research.sector_paper import paper_capture, paper_update


def _sector_paper_tick() -> None:
    today = date.today()
    build_sector_returns(
        (today - timedelta(days=7)).isoformat(),
        today.isoformat(),
    )
    for horizon in (1, 3, 5):
        paper_capture(days=3, horizon=horizon)
        paper_update(horizon=horizon)


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
    scheduler.add_job(
        _sector_paper_tick,
        CronTrigger(hour=23, minute=30, timezone=settings.tz),
        id="sector-paper",
        max_instances=1,
        coalesce=True,
    )
    return scheduler
