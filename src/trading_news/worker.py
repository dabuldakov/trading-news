from __future__ import annotations

import logging

from .db import close_pool, migrate
from .scheduler import build_scheduler

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    migrate()
    scheduler = build_scheduler()
    logger.info("news worker started")
    try:
        scheduler.start()
    finally:
        close_pool()


if __name__ == "__main__":
    main()
