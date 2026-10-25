"""Shared statistics helpers for the news research modules."""

from __future__ import annotations

import math
import random
from collections import defaultdict
from statistics import mean as stat_mean
from statistics import stdev

EVENT_GROUPS: dict[str, set[str]] = {
    "corporate": {
        "earnings",
        "dividend",
        "m&a",
        "contract",
        "production",
        "management",
        "rating",
    },
    "macro": {
        "rate",
        "cpi",
        "gdp",
        "budget",
        "regulation",
        "sanctions",
    },
}


def sentiment_label(sentiment: float | None) -> str:
    value = sentiment or 0.0
    if value > 0:
        return "pos"
    if value < 0:
        return "neg"
    return "neutral"


def source_key(source: str | None) -> str:
    """Collapse ``wayback:rbc`` / ``telegram:rbc`` to ``rbc``.

    Historical backfill and live feeds then share the same marker key.
    """
    text = (source or "unknown").strip()
    return text.split(":", 1)[-1] if ":" in text else text


def marker_key(
    event_type: str | None,
    sentiment: float | None,
    source: str | None,
    mode: str = "source",
) -> str:
    label = sentiment_label(sentiment)
    if mode == "event":
        return f"{event_type or 'other'}|{label}"
    if mode == "group":
        return f"{event_group(event_type)}|{label}"
    return f"{event_type or 'other'}|{label}|{source_key(source)}"


def event_group(event_type: str | None) -> str:
    value = (event_type or "other").strip().lower()
    for group, members in EVENT_GROUPS.items():
        if value in members:
            return group
    return "other"


def stats(values: list[float]) -> dict:
    if not values:
        return {"n": 0, "mean": 0.0, "t": 0.0, "hit": 0.0}
    avg = stat_mean(values)
    std = stdev(values) if len(values) > 1 else 0.0
    t = avg / (std / math.sqrt(len(values))) if std > 0 else 0.0
    return {
        "n": len(values),
        "mean": avg,
        "t": t,
        "hit": sum(1 for v in values if v > 0) / len(values),
    }


def bootstrap_ci(
    rows: list[tuple[float, object]],
    *,
    n_boot: int = 2000,
    alpha: float = 0.05,
    seed: int = 7,
) -> tuple[float, float]:
    """Cluster bootstrap CI for the mean.

    ``rows`` are ``(value, cluster_id)`` pairs. Resampling whole clusters keeps
    multiple tickers of the same article from being treated as independent.
    """
    if len(rows) < 2:
        return (0.0, 0.0)
    clusters: dict[object, list[float]] = defaultdict(list)
    for value, cluster in rows:
        clusters[cluster].append(float(value))
    ids = list(clusters)
    if len(ids) < 2:
        return (0.0, 0.0)
    rng = random.Random(seed)
    means = []
    for _ in range(n_boot):
        sample: list[float] = []
        for _ in range(len(ids)):
            sample.extend(clusters[rng.choice(ids)])
        means.append(stat_mean(sample))
    means.sort()
    lo = means[int(alpha / 2 * n_boot)]
    hi = means[min(int((1 - alpha / 2) * n_boot), n_boot - 1)]
    return (lo, hi)
