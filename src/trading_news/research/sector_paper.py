"""Paper-монитор отскока после негативных новостей сектора.

Правило: если по сектору за день средний sentiment < 0, на следующей торговой
сессии покупаем equal-weight корзину сектора и держим ``horizon`` дней.
Никаких ордеров: только запись сигналов и фактических forward-результатов.
"""

from __future__ import annotations

import argparse
import logging
from collections import defaultdict
from datetime import date, timedelta

from ..db import pool
from .sector_flow import MARKET, _load_flow, _load_returns, forward_records
from .stats import stats

logger = logging.getLogger(__name__)

COST = 0.001


def _trading_days(returns: dict[tuple[date, str], float]) -> list[date]:
    return sorted({day for (day, sector) in returns if sector != MARKET})


def _forward(
    returns: dict[tuple[date, str], float],
    days: list[date],
    signal_day: date,
    sector: str,
    horizon: int,
) -> dict | None:
    index = {day: position for position, day in enumerate(days)}
    position = index.get(signal_day)
    if position is None:
        return None
    start = position + 1
    if start + horizon > len(days):
        return None
    forward = 1.0
    benchmark = 1.0
    for offset in range(start, start + horizon):
        value = returns.get((days[offset], sector))
        bench = returns.get((days[offset], MARKET))
        if value is None or bench is None:
            return None
        forward *= 1.0 + value
        benchmark *= 1.0 + bench
    return {
        "entry_day": days[start],
        "ret": forward - 1.0,
        "excess": (forward - 1.0) - (benchmark - 1.0),
    }


def paper_capture(days: int = 7, horizon: int = 1) -> int:
    """Записать негативные sector-day сигналы за последние ``days`` дней."""
    end = date.today()
    start = end - timedelta(days=days)
    returns = _load_returns(start.isoformat(), end.isoformat())
    days_list = _trading_days(returns)
    flow = _load_flow(start.isoformat(), end.isoformat())
    written = 0
    with pool().connection() as conn:
        for (signal_day, sector), values in flow.items():
            if values["sentiment"] >= 0:
                continue
            forward = _forward(returns, days_list, signal_day, sector, horizon)
            conn.execute(
                """
                INSERT INTO sector_paper_trades
                    (signal_day, sector, sentiment, n_news, horizon, entry_day)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (signal_day, sector, horizon) DO UPDATE SET
                    sentiment = EXCLUDED.sentiment,
                    n_news = EXCLUDED.n_news,
                    entry_day = COALESCE(
                        sector_paper_trades.entry_day, EXCLUDED.entry_day
                    ),
                    updated_at = now()
                """,
                (
                    signal_day,
                    sector,
                    values["sentiment"],
                    values["n"],
                    horizon,
                    forward["entry_day"] if forward else None,
                ),
            )
            written += 1
        conn.commit()
    return written


def paper_update(horizon: int = 1) -> int:
    """Закрыть созревшие сделки фактическими forward-доходностями."""
    with pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT id, signal_day, sector
            FROM sector_paper_trades
            WHERE status = 'open' AND horizon = %s
            """,
            (horizon,),
        ).fetchall()
    if not rows:
        return 0
    start = min(row[1] for row in rows).isoformat()
    end = date.today().isoformat()
    returns = _load_returns(start, end)
    days_list = _trading_days(returns)
    closed = 0
    with pool().connection() as conn:
        for trade_id, signal_day, sector in rows:
            forward = _forward(returns, days_list, signal_day, sector, horizon)
            if forward is None:
                continue
            conn.execute(
                """
                UPDATE sector_paper_trades
                SET entry_day = %s, ret = %s, excess = %s,
                    status = 'closed', updated_at = now()
                WHERE id = %s
                """,
                (
                    forward["entry_day"],
                    forward["ret"],
                    forward["excess"],
                    trade_id,
                ),
            )
            closed += 1
        conn.commit()
    return closed


def _closed_rows(horizon: int) -> list[tuple]:
    with pool().connection() as conn:
        return conn.execute(
            """
            SELECT signal_day, sector, excess
            FROM sector_paper_trades
            WHERE status = 'closed' AND horizon = %s AND excess IS NOT NULL
            ORDER BY signal_day
            """,
            (horizon,),
        ).fetchall()


def _print_report(horizon: int) -> None:
    rows = _closed_rows(horizon)
    values = [float(row[2]) - COST for row in rows]
    overall = stats(values)
    with pool().connection() as conn:
        open_count = conn.execute(
            """
            SELECT count(*) FROM sector_paper_trades
            WHERE status = 'open' AND horizon = %s
            """,
            (horizon,),
        ).fetchone()[0]
    print(
        f"SECTOR PAPER h={horizon}d closed={overall['n']} open={open_count} "
        f"mean={overall['mean'] * 100:+.3f}% (t {overall['t']:+.1f}) "
        f"hit={overall['hit']:.0%}"
    )
    by_sector: dict[str, list[float]] = defaultdict(list)
    by_month: dict[str, list[float]] = defaultdict(list)
    for signal_day, sector, excess in rows:
        net = float(excess) - COST
        by_sector[sector].append(net)
        by_month[signal_day.strftime("%Y-%m")].append(net)
    for sector, sector_values in sorted(
        by_sector.items(), key=lambda item: len(item[1]), reverse=True
    ):
        item = stats(sector_values)
        print(
            f"  {sector:<12} n={item['n']:<4} {item['mean'] * 100:+.3f}% "
            f"(t {item['t']:+.1f}) hit {item['hit']:.0%}"
        )
    print("  -- by month --")
    for month, month_values in sorted(by_month.items()):
        item = stats(month_values)
        print(
            f"  {month:<8} n={item['n']:<3} {item['mean'] * 100:+.3f}% "
            f"(t {item['t']:+.1f}) hit {item['hit']:.0%}"
        )


def paper_replay(
    start: str,
    end: str,
    *,
    horizon: int = 1,
    per_trade_pct: float = 0.1,
    capital: float = 1_000_000.0,
    cost: float = COST,
) -> dict:
    """Исторический replay фиксированного правила (long-only, без капитала)."""
    returns = _load_returns(start, end)
    flow = _load_flow(start, end)
    records = forward_records(returns, flow, horizon)
    trades = [row for row in records if row["sentiment"] < 0]
    trades.sort(key=lambda row: row["day"])
    equity = capital
    peak = capital
    max_drawdown = 0.0
    pnls = []
    by_month: dict[str, list[float]] = defaultdict(list)
    by_sector: dict[str, list[float]] = defaultdict(list)
    for row in trades:
        net = row["excess"] - cost
        contribution = per_trade_pct * net
        equity *= 1.0 + contribution
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, 1.0 - equity / peak)
        pnls.append(net)
        by_month[row["day"].strftime("%Y-%m")].append(net)
        by_sector[row["sector"]].append(net)
    overall = stats(pnls)
    return {
        "trades": len(trades),
        "mean": overall["mean"],
        "t": overall["t"],
        "hit": overall["hit"],
        "total_return": equity / capital - 1.0 if capital else 0.0,
        "max_drawdown": max_drawdown,
        "final_equity": equity,
        "by_month": {
            key: stats(values) for key, values in by_month.items()
        },
        "by_sector": {
            key: stats(values) for key, values in by_sector.items()
        },
    }


def _print_replay(result: dict, *, start: str, end: str, horizon: int) -> None:
    print(
        f"SECTOR REPLAY h={horizon}d {start}..{end} trades={result['trades']} "
        f"mean={result['mean'] * 100:+.3f}% (t {result['t']:+.1f}) "
        f"hit={result['hit']:.0%} total={result['total_return'] * 100:+.2f}% "
        f"maxDD={result['max_drawdown'] * 100:.2f}%"
    )
    print("  -- by month --")
    for month, item in sorted(result["by_month"].items()):
        print(
            f"  {month:<8} n={item['n']:<3} {item['mean'] * 100:+.3f}% "
            f"(t {item['t']:+.1f}) hit {item['hit']:.0%}"
        )
    print("  -- by sector --")
    for sector, item in sorted(
        result["by_sector"].items(), key=lambda kv: kv[1]["n"], reverse=True
    ):
        print(
            f"  {sector:<12} n={item['n']:<4} {item['mean'] * 100:+.3f}% "
            f"(t {item['t']:+.1f}) hit {item['hit']:.0%}"
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="Sector negative-news paper monitor")
    parser.add_argument("--capture", type=int, metavar="DAYS")
    parser.add_argument("--update", action="store_true")
    parser.add_argument("--report", action="store_true")
    parser.add_argument(
        "--replay", nargs=2, metavar=("START", "END")
    )
    parser.add_argument("--horizon", type=int, default=1)
    args = parser.parse_args()
    if args.replay:
        _print_replay(
            paper_replay(args.replay[0], args.replay[1], horizon=args.horizon),
            start=args.replay[0],
            end=args.replay[1],
            horizon=args.horizon,
        )
    if args.capture is not None:
        print({"captured": paper_capture(args.capture, horizon=args.horizon)})
    if args.update:
        print({"closed": paper_update(horizon=args.horizon)})
    if args.report:
        _print_report(args.horizon)
