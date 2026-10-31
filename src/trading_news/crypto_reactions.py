"""Build crypto price reactions from OKX public candles.

Stored in the isolated crypto-news DB.  Horizons are 5m, 1h, 4h and 1d;
abnormal returns are measured versus BTC.
"""
from __future__ import annotations

import argparse
import bisect
import json
import logging
import time
from datetime import UTC
from pathlib import Path

import httpx

from .db import pool
from .status import mark_stage

logger = logging.getLogger(__name__)
BASE = "https://www.okx.com/api/v5/market/candles"
BARS = {"5m": "5m", "1h": "1H", "4h": "4H", "1d": "1D"}


def _candles(client: httpx.Client, symbol: str, bar: str, limit: int) -> list[tuple[int, float]]:
    inst = symbol.replace(":", "/").replace("/", "-")
    r = client.get(BASE, params={"instId": inst, "bar": BARS[bar], "limit": limit})
    r.raise_for_status()
    data = r.json().get("data") or []
    out = []
    for row in data:
        try:
            out.append((int(row[0]), float(row[4])))
        except (IndexError, TypeError, ValueError):
            continue
    out.sort()
    return out


def _ret_at(candles: list[tuple[int, float]], ts: int, step: int) -> float | None:
    if not candles:
        return None
    idx = bisect.bisect_left([x[0] for x in candles], ts)
    if idx <= 0 or idx + step >= len(candles):
        return None
    return candles[idx + step][1] / candles[idx][1] - 1


def run(limit: int = 1000) -> dict:
    with pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT a.id, a.published_at, t.ticker
            FROM news_articles a
            JOIN news_tickers t ON t.article_id = a.id
            WHERE t.relevance >= 1.0
            ORDER BY a.published_at DESC
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
    cache: dict[tuple[str, str], list[tuple[int, float]]] = {}
    built = 0
    with httpx.Client(timeout=20, trust_env=True, headers={"User-Agent": "trading-news-crypto/0.1"}) as client:
        btc = {bar: _candles(client, "BTC:USDT", bar, 300) for bar in BARS}
        for article_id, published, ticker in rows:
            ts = int(published.timestamp() * 1000)
            values = {}
            try:
                for bar in BARS:
                    key = (ticker, bar)
                    if key not in cache:
                        cache[key] = _candles(client, ticker, bar, 300)
                        time.sleep(0.05)
                    values[bar] = _ret_at(cache[key], ts, 1)
            except Exception as exc:  # noqa: BLE001
                logger.warning("crypto reaction %s: %s", ticker, exc)
                continue
            if values["1d"] is None:
                continue
            btc1d = _ret_at(btc["1d"], ts, 1)
            btc4h = _ret_at(btc["4h"], ts, 1)
            abnormal_1d = values["1d"] - btc1d if btc1d is not None else None
            abnormal_4h = values["4h"] - btc4h if (values["4h"] is not None and btc4h is not None) else None
            with pool().connection() as conn:
                conn.execute(
                    """
                    INSERT INTO news_reactions
                        (article_id, ticker, t0, ret_5m, ret_1h, ret_4h, ret_1d,
                         abnormal_ret, abnormal_4h, costs)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (article_id, ticker) DO UPDATE SET
                        t0=EXCLUDED.t0, ret_5m=EXCLUDED.ret_5m,
                        ret_1h=EXCLUDED.ret_1h, ret_4h=EXCLUDED.ret_4h,
                        ret_1d=EXCLUDED.ret_1d, abnormal_ret=EXCLUDED.abnormal_ret,
                        abnormal_4h=EXCLUDED.abnormal_4h, costs=EXCLUDED.costs
                    """,
                    (article_id, ticker, published, values["5m"], values["1h"],
                     values["4h"], values["1d"], abnormal_1d, abnormal_4h, 0.002),
                )
                conn.commit()
            built += 1
    try:
        mark_stage("reactions", "ok", f"reactions={built}")
    except Exception:  # noqa: BLE001
        pass
    return {"rows": len(rows), "built": built}


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=1000)
    args = parser.parse_args()
    print(run(args.limit))


if __name__ == "__main__":
    main()
