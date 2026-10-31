from __future__ import annotations

from datetime import UTC, datetime

from trading_news.status import mark_stage, snapshot


def test_mark_and_snapshot() -> None:
    started = datetime(2026, 10, 31, 8, 0, tzinfo=UTC)
    mark_stage("signals", "ok", "signals=7", started_at=started)
    data = snapshot()
    assert data["profile"] == "ru"
    assert data["health"] in ("ok", "warn")
    assert data["pipeline"]["stages"]["signals"]["status"] == "ok"
    assert data["pipeline"]["last_signals"] is not None
    assert data["daily"]


def test_mark_stage_error_is_visible() -> None:
    mark_stage("reactions", "error", "okx timeout")
    data = snapshot()
    assert data["health"] == "error"
    assert data["pipeline"]["errors"][0]["stage"] == "reactions"
