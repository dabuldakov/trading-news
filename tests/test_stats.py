from __future__ import annotations

import pytest

from trading_news.research.stats import (
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


def test_sentiment_label() -> None:
    assert sentiment_label(0.5) == "pos"
    assert sentiment_label(-0.5) == "neg"
    assert sentiment_label(0.0) == "neutral"
    assert sentiment_label(None) == "neutral"


def test_source_key_collapses_backfill_prefix() -> None:
    assert source_key("wayback:rbc") == "rbc"
    assert source_key("telegram:tass") == "tass"
    assert source_key("rbc") == "rbc"
    assert source_key(None) == "unknown"
    assert source_key("  kommersant ") == "kommersant"


def test_event_group() -> None:
    assert event_group("dividend") == "corporate"
    assert event_group("m&a") == "corporate"
    assert event_group("rate") == "macro"
    assert event_group("sanctions") == "macro"
    assert event_group("other") == "other"
    assert event_group(None) == "other"
    assert event_group("unknown-type") == "other"


def test_marker_key_modes() -> None:
    assert marker_key("dividend", 1, "wayback:rbc") == "dividend|pos|rbc"
    assert marker_key("dividend", -1, "rbc", "event") == "dividend|neg"
    assert marker_key("rate", 0, "tass", "group") == "macro|neutral"
    assert marker_key("other", 0, None) == "other|neutral|unknown"


def test_stats_empty_and_single() -> None:
    assert stats([]) == {"n": 0, "mean": 0.0, "t": 0.0, "hit": 0.0}
    single = stats([0.02])
    assert single["n"] == 1
    assert single["mean"] == pytest.approx(0.02)
    assert single["t"] == 0.0
    assert single["hit"] == 1.0


def test_stats_zero_variance() -> None:
    result = stats([0.01, 0.01, 0.01])
    assert result["mean"] == pytest.approx(0.01)
    assert result["t"] == 0.0
    assert result["hit"] == 1.0


def test_stats_t_and_hit() -> None:
    result = stats([0.01, 0.02, 0.03, -0.01])
    assert result["n"] == 4
    assert result["mean"] == pytest.approx(0.0125)
    assert result["t"] > 0
    assert result["hit"] == pytest.approx(0.75)


def test_bootstrap_ci_needs_two_clusters() -> None:
    assert bootstrap_ci([]) == (0.0, 0.0)
    assert bootstrap_ci([(0.01, "a")]) == (0.0, 0.0)
    assert bootstrap_ci([(0.01, "a"), (0.02, "a")]) == (0.0, 0.0)


def test_bootstrap_ci_is_deterministic_and_brackets_mean() -> None:
    rows = [(index / 100.0, index % 4) for index in range(40)]
    first = bootstrap_ci(rows, n_boot=300, seed=11)
    second = bootstrap_ci(rows, n_boot=300, seed=11)
    assert first == second
    low, high = first
    assert low <= high
    assert low <= 0.195 <= high


def test_bootstrap_ci_resamples_clusters() -> None:
    # Cluster "a" has two extreme values; resampling rows would narrow the CI.
    rows = [
        (2.0, "a"),
        (2.0, "a"),
        (0.0, "b"),
        (0.0, "b"),
        (-2.0, "c"),
        (-2.0, "c"),
    ]
    low, high = bootstrap_ci(rows, n_boot=500, seed=3)
    assert low < 0 < high
    assert low < -0.4
    assert high > 0.4


def test_t_pvalue_and_bh() -> None:
    assert t_pvalue(0.0, 10) == pytest.approx(1.0)
    assert t_pvalue(3.0, 30) < 0.01
    assert bh_qvalues([]) == []
    qvalues = bh_qvalues([0.01, 0.02, 0.03, 0.04])
    assert all(0 <= q <= 1 for q in qvalues)
    assert qvalues == pytest.approx([0.04, 0.04, 0.04, 0.04])
    monotonic = bh_qvalues([0.001, 0.01, 0.5])
    assert monotonic[0] <= monotonic[1] <= monotonic[2]


def test_permutation_spread() -> None:
    spread, pvalue = permutation_spread([1.0, 2.0, 3.0], [0.0, 0.0, 0.0])
    assert spread == pytest.approx(2.0)
    assert 0 < pvalue <= 1
    same, flat = permutation_spread([0.01, 0.02], [0.01, 0.02])
    assert same == pytest.approx(0.0)
    assert flat == pytest.approx(1.0)
    assert permutation_spread([1.0], [0.0]) == (0.0, 1.0)
