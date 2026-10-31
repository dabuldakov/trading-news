"""Out-of-sample проверка новостных маркеров.

Маркеры строятся только на обучающем периоде (``t0 < train_end``), затем
оцениваются на тестовом (``published_at >= test_start``). Это убирает главную
проблему in-sample: сигнал не «видел» реакцию, на которой посчитан.

Дополнительно поддерживаются:
* горизонты 1/3/5 торговых дней (abnormal return vs IMOEX);
* фильтры по ликвидности (ADV), достоверности источника и группе события;
* кластерный bootstrap (кластер — статья, а не пара article/ticker);
* walk-forward по нескольким последовательным тестовым окнам.
"""

from __future__ import annotations

import argparse
import calendar
import logging
import math
from collections import defaultdict
from datetime import date, timedelta
from statistics import mean as stat_mean
from statistics import stdev

from ..db import pool
from .stats import (
    bh_qvalues,
    bootstrap_ci,
    event_group,
    marker_key,
    permutation_spread,
    sentiment_label,
    source_key,
    stats,
    t_pvalue,
)

logger = logging.getLogger(__name__)

COST = 0.001
MIN_MARKER_N = 10
MIN_T = 2.0

ABNORMAL = {1: "abnormal_ret", 3: "abnormal_3d", 5: "abnormal_5d"}
RETURN = {1: "ret_1d", 3: "ret_3d", 5: "ret_5d"}


def _train_markers(
    conn,
    train_end: str,
    horizon: int,
    *,
    require_abnormal: bool,
    key_mode: str,
) -> dict[str, dict]:
    abnormal = f"r.{ABNORMAL[horizon]}"
    raw = f"r.{RETURN[horizon]}"
    expr = abnormal if require_abnormal else f"COALESCE({abnormal}, {raw})"
    rows = conn.execute(
        f"""
        SELECT n.event_type, n.sentiment, s.name AS source, {expr} AS reaction
        FROM news_reactions r
        JOIN news_analysis n ON n.article_id = r.article_id
        JOIN news_articles a ON a.id = r.article_id
        LEFT JOIN news_sources s ON s.id = a.source_id
        WHERE r.t0 < %s
          AND {expr} IS NOT NULL
        """,
        (train_end,),
    ).fetchall()
    groups: dict[str, list[float]] = {}
    for event_type, sentiment, source, reaction in rows:
        key = marker_key(event_type, sentiment, source, key_mode)
        groups.setdefault(key, []).append(float(reaction))
    return {key: stats(values) for key, values in groups.items()}


def _group_stats(rows: list[dict], field: str) -> dict[str, dict]:
    buckets: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        buckets[row[field]].append(row["pnl"])
    return {key: stats(values) for key, values in buckets.items()}


def event_study(
    *,
    horizon: int = 1,
    start: str | None = None,
    end: str | None = None,
    require_abnormal: bool = True,
    demean_by_day: bool = False,
    demean_by_sector_day: bool = False,
    intraday: str | None = None,
) -> dict:
    """Средняя аномальная доходность по (группа события, знак) без торговли."""
    if intraday is not None:
        if intraday not in ("1m", "5m", "30m", "1h"):
            raise ValueError("intraday must be one of 1m/5m/30m/1h")
        expr = f"r.ret_{intraday}"
        horizon_label = intraday
    else:
        if horizon not in ABNORMAL:
            raise ValueError(f"horizon must be one of {sorted(ABNORMAL)}")
        abnormal = f"r.{ABNORMAL[horizon]}"
        raw = f"r.{RETURN[horizon]}"
        expr = abnormal if require_abnormal else f"COALESCE({abnormal}, {raw})"
        horizon_label = horizon
    clauses = [f"{expr} IS NOT NULL"]
    params: list = []
    if start is not None:
        clauses.append("a.published_at >= %s")
        params.append(start)
    if end is not None:
        clauses.append("a.published_at < %s")
        params.append(end)
    with pool().connection() as conn:
        rows = conn.execute(
            f"""
            SELECT a.id, a.published_at, n.event_type, n.sentiment,
                   co.sector, {expr}
            FROM news_reactions r
            JOIN news_analysis n ON n.article_id = r.article_id
            JOIN news_articles a ON a.id = r.article_id
            JOIN instruments i ON i.ticker = r.ticker
            LEFT JOIN companies co ON co.id = i.company_id
            WHERE {" AND ".join(clauses)}
            """,
            params,
        ).fetchall()
    day_values: dict = defaultdict(list)
    sector_day_values: dict = defaultdict(list)
    for _id, published, _et, _sent, sector, value in rows:
        day_values[published.date()].append(float(value))
        if sector:
            sector_day_values[(published.date(), sector)].append(float(value))
    day_mean = {key: stat_mean(values) for key, values in day_values.items()}
    sector_day_mean = {
        key: stat_mean(values) for key, values in sector_day_values.items()
    }
    groups: dict[str, list[tuple[float, object]]] = defaultdict(list)
    for article_id, published, event_type, sentiment, sector, value in rows:
        adjusted = float(value)
        if demean_by_sector_day:
            reference = (
                sector_day_mean.get((published.date(), sector)) if sector else None
            )
            if reference is None:
                reference = day_mean[published.date()]
            adjusted -= reference
        elif demean_by_day:
            adjusted -= day_mean[published.date()]
        key = f"{event_group(event_type)}|{sentiment_label(sentiment)}"
        groups[key].append((adjusted, article_id))
    out = {}
    for key, values in groups.items():
        item = stats([value for value, _ in values])
        item["ci_low"], item["ci_high"] = bootstrap_ci(values)
        out[key] = item
    spreads = {}
    for group in ("corporate", "macro", "other"):
        positive = [value for value, _ in groups.get(f"{group}|pos", [])]
        negative = [value for value, _ in groups.get(f"{group}|neg", [])]
        spread, pvalue = permutation_spread(positive, negative)
        spreads[group] = {
            "spread": spread,
            "p": pvalue,
            "pos_n": len(positive),
            "neg_n": len(negative),
        }
    return {
        "horizon": horizon_label,
        "start": start,
        "end": end,
        "n": len(rows),
        "demeaned": (
            "sector-day" if demean_by_sector_day else "day" if demean_by_day else False
        ),
        "groups": out,
        "spreads": spreads,
    }


def _print_study(result: dict) -> None:
    window = f"{result['start'] or 'start'}..{result['end'] or 'now'}"
    mode = f"demeaned-by-{result['demeaned']}" if result.get("demeaned") else "raw"
    horizon_label = result["horizon"]
    if isinstance(horizon_label, int):
        horizon_label = f"{horizon_label}d"
    print(f"EVENT STUDY h={horizon_label} window={window} n={result['n']} ({mode})")
    for key, st in sorted(
        result["groups"].items(), key=lambda kv: kv[1]["n"], reverse=True
    ):
        print(
            f"  {key:<20} n={st['n']:<5} {st['mean'] * 100:+.3f}% "
            f"(t {st['t']:+.1f}) hit {st['hit']:.0%} "
            f"CI95=[{st['ci_low'] * 100:+.3f}%, {st['ci_high'] * 100:+.3f}%]"
        )
    print("  -- long/short spread (pos - neg, permutation p) --")
    for group, item in result["spreads"].items():
        print(
            f"  {group:<12} {item['spread'] * 100:+.3f}% "
            f"p={item['p']:.3f} (pos n={item['pos_n']}, neg n={item['neg_n']})"
        )


def _with_ci(result: dict, rows: list[dict], cluster: str) -> dict:
    low, high = bootstrap_ci([(row["pnl"], row[cluster]) for row in rows])
    result["ci_low"] = low
    result["ci_high"] = high
    return result


def evaluate(
    train_end: str,
    test_start: str,
    *,
    test_end: str | None = None,
    horizon: int = 1,
    cost: float = COST,
    min_edge: float = 0.001,
    min_marker_n: int = MIN_MARKER_N,
    min_t: float = MIN_T,
    min_adv: float = 0.0,
    min_credibility: float = 0.0,
    only_group: str | None = None,
    require_abnormal: bool = True,
    key_mode: str = "source",
) -> dict:
    if horizon not in ABNORMAL:
        raise ValueError(f"horizon must be one of {sorted(ABNORMAL)}")
    abnormal = f"r.{ABNORMAL[horizon]}"
    raw = f"r.{RETURN[horizon]}"

    with pool().connection() as conn:
        markers = _train_markers(
            conn,
            train_end,
            horizon,
            require_abnormal=require_abnormal,
            key_mode=key_mode,
        )
        where = "a.published_at >= %s"
        params: list = [test_start]
        if test_end is not None:
            where += " AND a.published_at < %s"
            params.append(test_end)
        rows = conn.execute(
            f"""
            SELECT a.id, a.published_at, t.ticker, n.event_type, n.sentiment,
                   s.name AS source, s.credibility, co.sector,
                   {abnormal}, {raw}, r.adv_rub
            FROM news_articles a
            JOIN news_tickers t ON t.article_id = a.id
            JOIN news_analysis n ON n.article_id = a.id
            JOIN news_reactions r
              ON r.article_id = a.id AND r.ticker = t.ticker
            LEFT JOIN news_sources s ON s.id = a.source_id
            LEFT JOIN instruments i ON i.ticker = t.ticker
            LEFT JOIN companies co ON co.id = i.company_id
            WHERE {where}
            """,
            params,
        ).fetchall()

    all_pairs: list[dict] = []
    signals: list[dict] = []
    for (
        article_id,
        published,
        ticker,
        event_type,
        sentiment,
        source,
        credibility,
        sector,
        abnormal_value,
        raw_value,
        adv,
    ) in rows:
        if credibility is not None and float(credibility) < min_credibility:
            continue
        group = event_group(event_type)
        if only_group is not None and group != only_group:
            continue
        if min_adv > 0 and (adv is None or float(adv) < min_adv):
            continue
        key = marker_key(event_type, sentiment, source, key_mode)
        marker = markers.get(key)
        if marker is None or marker["n"] < min_marker_n:
            continue
        if require_abnormal:
            reaction = abnormal_value
        else:
            reaction = abnormal_value if abnormal_value is not None else raw_value
        if reaction is None:
            continue
        side = 1 if marker["mean"] > cost else -1
        pnl = side * float(reaction) - cost
        row = {
            "article_id": article_id,
            "published": published,
            "marker": key,
            "group": group,
            "source": source_key(source),
            "sector": sector or "unknown",
            "ticker": ticker,
            "side": side,
            "reaction": float(reaction),
            "pnl": pnl,
        }
        all_pairs.append(row)
        edge = marker["mean"] - cost
        if abs(edge) < min_edge or abs(marker["t"]) < min_t:
            continue
        signals.append(row)

    overall_all = _with_ci(
        stats([row["pnl"] for row in all_pairs]), all_pairs, "article_id"
    )
    overall = _with_ci(stats([row["pnl"] for row in signals]), signals, "article_id")

    by_marker_all = _group_stats(all_pairs, "marker")
    by_group_all = _group_stats(all_pairs, "group")
    by_source_all = _group_stats(all_pairs, "source")
    by_sector_all = _group_stats(all_pairs, "sector")
    by_marker_signal = _group_stats(signals, "marker")
    marker_keys = list(by_marker_all)
    pvalues = [
        t_pvalue(by_marker_all[key]["t"], by_marker_all[key]["n"])
        for key in marker_keys
    ]
    for key, qvalue in zip(marker_keys, bh_qvalues(pvalues), strict=True):
        by_marker_all[key]["q"] = qvalue
    markers_fdr10 = sum(
        1
        for key in marker_keys
        if by_marker_all[key]["q"] <= 0.10 and by_marker_all[key]["n"] >= 20
    )

    return {
        "train_end": train_end,
        "test_start": test_start,
        "test_end": test_end,
        "horizon": horizon,
        "key_mode": key_mode,
        "markers_train": len(markers),
        "markers_fdr10": markers_fdr10,
        "pairs": len(all_pairs),
        "signals_oos": len(signals),
        "overall": overall,
        "overall_all": overall_all,
        "by_marker_all": by_marker_all,
        "by_marker_signal": by_marker_signal,
        "by_group_all": by_group_all,
        "by_source_all": by_source_all,
        "by_sector_all": by_sector_all,
        "pairs_rows": all_pairs,
        "signals_rows": signals,
    }


def _print(summary: dict, *, top: int = 10) -> None:
    window = f"test>={summary['test_start']}"
    if summary["test_end"]:
        window += f" <{summary['test_end']}"
    print(
        f"OOS h={summary['horizon']}d key={summary['key_mode']} "
        f"train<{summary['train_end']} {window} | "
        f"маркеров {summary['markers_train']} (FDR10 {summary['markers_fdr10']}) "
        f"| пар {summary['pairs']} | сигналов {summary['signals_oos']}"
    )
    for name, st in (
        ("SIGNAL", summary["overall"]),
        ("ALL   ", summary["overall_all"]),
    ):
        print(
            f"  {name} n={st['n']:<4} mean={st['mean'] * 100:+.3f}% "
            f"(t {st['t']:+.1f}) hit {st['hit']:.0%} "
            f"CI95=[{st['ci_low'] * 100:+.3f}%, {st['ci_high'] * 100:+.3f}%]"
        )
    print("  -- groups --")
    for key, st in sorted(
        summary["by_group_all"].items(), key=lambda kv: kv[1]["n"], reverse=True
    ):
        print(
            f"  {key:<12} n={st['n']:<4} {st['mean'] * 100:+.3f}% "
            f"(t {st['t']:+.1f}) hit {st['hit']:.0%}"
        )
    print("  -- sources --")
    for key, st in sorted(
        summary["by_source_all"].items(), key=lambda kv: kv[1]["n"], reverse=True
    )[:top]:
        print(f"  {key:<16} n={st['n']:<4} {st['mean'] * 100:+.3f}% (t {st['t']:+.1f})")
    sectors = sorted(
        summary["by_sector_all"].items(), key=lambda kv: kv[1]["n"], reverse=True
    )[:top]
    if sectors:
        print("  -- sectors --")
        for key, st in sectors:
            print(
                f"  {key:<16} n={st['n']:<4} {st['mean'] * 100:+.3f}% "
                f"(t {st['t']:+.1f})"
            )
    print("  -- markers (all pairs, top by mean) --")
    for key, st in sorted(
        summary["by_marker_all"].items(), key=lambda kv: kv[1]["mean"], reverse=True
    )[:top]:
        print(
            f"  {key:<28} n={st['n']:<4} {st['mean'] * 100:+.3f}% "
            f"(t {st['t']:+.1f}, q {st.get('q', 1.0):.2f}) hit {st['hit']:.0%}"
        )


def portfolio_backtest(
    train_end: str,
    test_start: str,
    *,
    test_end: str | None = None,
    horizon: int = 1,
    key_mode: str = "source",
    cost: float = COST,
    min_edge: float = 0.001,
    min_marker_n: int = MIN_MARKER_N,
    min_t: float = MIN_T,
    min_adv: float = 0.0,
    min_credibility: float = 0.0,
    only_group: str | None = None,
    require_abnormal: bool = True,
    capital: float = 1_000_000.0,
    per_trade_pct: float = 0.1,
    max_positions: int = 10,
    long_short: bool = True,
) -> dict:
    """Портфельный бэктест сигналов: капитал, перекрытие, издержки, drawdown."""
    summary = evaluate(
        train_end,
        test_start,
        test_end=test_end,
        horizon=horizon,
        key_mode=key_mode,
        cost=cost,
        min_edge=min_edge,
        min_marker_n=min_marker_n,
        min_t=min_t,
        min_adv=min_adv,
        min_credibility=min_credibility,
        only_group=only_group,
        require_abnormal=require_abnormal,
    )
    trades = [row for row in summary["signals_rows"] if long_short or row["side"] > 0]
    trades.sort(key=lambda row: row["published"])
    capital = float(capital)
    per_trade_pct = max(0.0, float(per_trade_pct))
    hold_days = max(1, horizon)
    active: list[dict] = []
    realized: list[tuple[object, float]] = []
    opened = 0

    def release(day) -> None:
        remaining = []
        for position in active:
            if position["exit"] <= day:
                contribution = position["pnl"] * per_trade_pct
                realized.append((position["exit"], contribution))
            else:
                remaining.append(position)
        active[:] = remaining

    for row in trades:
        release(row["published"])
        if len(active) >= max_positions:
            continue
        exit_at = row["published"] + timedelta(days=round(hold_days * 1.45) + 1)
        active.append({"exit": exit_at, "pnl": row["pnl"]})
        opened += 1
    if active:
        release(max(position["exit"] for position in active) + timedelta(days=1))

    equity = capital
    peak = capital
    max_drawdown = 0.0
    daily: dict = defaultdict(list)
    for when, contribution in sorted(realized, key=lambda item: item[0]):
        equity *= 1.0 + contribution
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, 1.0 - equity / peak)
        daily[when.date()].append(contribution)

    day_returns = []
    for _day, contributions in sorted(daily.items()):
        growth = 1.0
        for contribution in contributions:
            growth *= 1.0 + contribution
        day_returns.append(growth - 1.0)
    mean_return = stat_mean(day_returns) if day_returns else 0.0
    std_return = stdev(day_returns) if len(day_returns) > 1 else 0.0
    sharpe = mean_return / std_return * math.sqrt(252.0) if std_return > 0 else 0.0
    pnls = [row["pnl"] for row in trades]
    return {
        "horizon": horizon,
        "key_mode": key_mode,
        "trades": len(trades),
        "opened": opened,
        "total_return": equity / capital - 1.0 if capital else 0.0,
        "sharpe": sharpe,
        "max_drawdown": max_drawdown,
        "hit": (sum(1 for pnl in pnls if pnl > 0) / len(pnls)) if pnls else 0.0,
        "avg_pnl": stat_mean(pnls) if pnls else 0.0,
        "final_equity": equity,
        "long_short": long_short,
        "max_positions": max_positions,
        "per_trade_pct": per_trade_pct,
    }


def _print_portfolio(result: dict) -> None:
    print(
        f"PORTFOLIO h={result['horizon']}d key={result['key_mode']} "
        f"{'long/short' if result['long_short'] else 'long-only'} "
        f"signals={result['trades']} opened={result['opened']}"
    )
    print(
        f"  total={result['total_return'] * 100:+.2f}% "
        f"sharpe={result['sharpe']:+.2f} "
        f"maxDD={result['max_drawdown'] * 100:.2f}% "
        f"hit={result['hit']:.0%} avg={result['avg_pnl'] * 100:+.3f}% "
        f"equity={result['final_equity']:.0f}"
    )


def _add_months(day: date, months: int) -> date:
    month = day.month - 1 + months
    year = day.year + month // 12
    month = month % 12 + 1
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last))


def walk_forward(
    start: str,
    end: str,
    step_months: int,
    **kwargs,
) -> dict:
    cursor = date.fromisoformat(start)
    stop = date.fromisoformat(end)
    if step_months < 1 or cursor >= stop:
        raise ValueError("walk-forward needs start<end and step>=1")
    folds: list[dict] = []
    pooled: list[dict] = []
    while cursor < stop:
        nxt = min(_add_months(cursor, step_months), stop)
        summary = evaluate(
            cursor.isoformat(),
            cursor.isoformat(),
            test_end=nxt.isoformat(),
            **kwargs,
        )
        folds.append(summary)
        pooled.extend(summary.pop("pairs_rows"))
        cursor = nxt
    aggregate = _with_ci(stats([row["pnl"] for row in pooled]), pooled, "article_id")
    return {"folds": folds, "pooled": aggregate, "pairs": len(pooled)}


def _print_walk(result: dict) -> None:
    print("WALK-FORWARD")
    print(
        f"  {'test window':<25} {'n':>5} {'mean':>9} {'t':>6} {'hit':>5} {'CI95':>20}"
    )
    for fold in result["folds"]:
        st = fold["overall_all"]
        window = f"{fold['test_start']}..{fold['test_end']}"
        print(
            f"  {window:<25} {st['n']:>5} {st['mean'] * 100:>+8.3f}% "
            f"{st['t']:>+6.1f} {st['hit']:>4.0%} "
            f"[{st['ci_low'] * 100:+.2f}%, {st['ci_high'] * 100:+.2f}%]"
        )
    st = result["pooled"]
    print(
        f"  {'POOLED':<25} {st['n']:>5} {st['mean'] * 100:>+8.3f}% "
        f"{st['t']:>+6.1f} {st['hit']:>4.0%} "
        f"[{st['ci_low'] * 100:+.2f}%, {st['ci_high'] * 100:+.2f}%]"
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="OOS news markers")
    parser.add_argument("--train-end", default="2026-04-01")
    parser.add_argument("--test-start")
    parser.add_argument("--test-end")
    parser.add_argument("--horizon", type=int, choices=sorted(ABNORMAL), default=1)
    parser.add_argument("--cost", type=float, default=COST)
    parser.add_argument("--min-edge", type=float, default=0.001)
    parser.add_argument("--min-marker-n", type=int, default=MIN_MARKER_N)
    parser.add_argument("--min-t", type=float, default=MIN_T)
    parser.add_argument("--min-adv", type=float, default=0.0)
    parser.add_argument("--min-credibility", type=float, default=0.0)
    parser.add_argument("--group", choices=["corporate", "macro", "other"])
    parser.add_argument(
        "--key-mode",
        choices=["source", "event", "group"],
        default="source",
    )
    parser.add_argument(
        "--allow-fallback",
        action="store_true",
        help="use raw return when abnormal is missing",
    )
    parser.add_argument(
        "--study",
        action="store_true",
        help="report average abnormal return by event group instead of trading PnL",
    )
    parser.add_argument(
        "--intraday",
        choices=["1m", "5m", "30m", "1h"],
        help="use intraday reaction column (study mode)",
    )
    parser.add_argument(
        "--study-demean",
        action="store_true",
        help="subtract same-day cross-sectional mean before the event study",
    )
    parser.add_argument(
        "--study-demean-sector",
        action="store_true",
        help="subtract same sector-and-day mean (sector-neutral event study)",
    )
    parser.add_argument(
        "--portfolio",
        action="store_true",
        help="run a capital-aware portfolio backtest instead of per-event stats",
    )
    parser.add_argument("--capital", type=float, default=1_000_000.0)
    parser.add_argument("--per-trade-pct", type=float, default=0.1)
    parser.add_argument("--max-positions", type=int, default=10)
    parser.add_argument("--long-only", action="store_true")
    parser.add_argument(
        "--walk-forward",
        nargs=3,
        metavar=("START", "END", "STEP_MONTHS"),
    )
    args = parser.parse_args()

    test_start = args.test_start or args.train_end
    common = {
        "horizon": args.horizon,
        "cost": args.cost,
        "min_edge": args.min_edge,
        "min_marker_n": args.min_marker_n,
        "min_t": args.min_t,
        "min_adv": args.min_adv,
        "min_credibility": args.min_credibility,
        "only_group": args.group,
        "require_abnormal": not args.allow_fallback,
        "key_mode": args.key_mode,
    }
    if args.study:
        _print_study(
            event_study(
                horizon=args.horizon,
                start=args.test_start,
                end=args.test_end,
                require_abnormal=not args.allow_fallback,
                demean_by_day=args.study_demean,
                demean_by_sector_day=args.study_demean_sector,
                intraday=args.intraday,
            )
        )
    elif args.portfolio:
        _print_portfolio(
            portfolio_backtest(
                args.train_end,
                test_start,
                test_end=args.test_end,
                horizon=args.horizon,
                key_mode=args.key_mode,
                cost=args.cost,
                min_edge=args.min_edge,
                min_marker_n=args.min_marker_n,
                min_t=args.min_t,
                min_adv=args.min_adv,
                min_credibility=args.min_credibility,
                only_group=args.group,
                require_abnormal=not args.allow_fallback,
                capital=args.capital,
                per_trade_pct=args.per_trade_pct,
                max_positions=args.max_positions,
                long_short=not args.long_only,
            )
        )
    elif args.walk_forward:
        start, end, step = args.walk_forward
        _print_walk(walk_forward(start, end, int(step), **common))
    else:
        _print(
            evaluate(
                args.train_end,
                test_start,
                test_end=args.test_end,
                **common,
            )
        )
