from __future__ import annotations

import logging

from .db import close_pool, migrate

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(migrate())
    close_pool()
