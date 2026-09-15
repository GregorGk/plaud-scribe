"""Transcription ceilings: monthly by default, optional daily circuit breaker."""

from datetime import datetime, timedelta, timezone

import pytest

from plaud_scribe.config import Config
from plaud_scribe.state import Store
from plaud_scribe.sync import Pipeline, cache_path, month_start


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "state.db") as store:
        yield store


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _record(store, rid, *, minutes, when=None):
    when = (when or _now()).isoformat(timespec="seconds")
    store.note_seen(rid, rid, when, minutes * 60)
    store.mark_done(
        rid,
        duration_seconds=minutes * 60,
        provider="elevenlabs",
        model_id="scribe_v2",
        cost_usd=0.1,
        transcribed_at=when,
    )


def _pipeline(cfg, store):
    return Pipeline(cfg, plaud=None, store=store)


def test_default_is_monthly_only(store):
    cfg = Config()
    assert cfg.limits.monthly_minutes == 2700
    assert cfg.limits.daily_minutes == 0

    budgets = _pipeline(cfg, store).budgets()
    assert [b.period for b in budgets] == ["month"]
    assert budgets[0].remaining_minutes == 2700


def test_monthly_budget_depletes_and_clamps(store):
    cfg = Config()
    pipeline = _pipeline(cfg, store)

    _record(store, "a", minutes=700)
    assert pipeline.budget().remaining_minutes == 2000

    _record(store, "b", minutes=2500)
    assert pipeline.budget().remaining_minutes == 0


def test_previous_month_does_not_count(store):
    cfg = Config()
    _record(store, "old", minutes=2700, when=month_start(_now()) - timedelta(days=1))
    assert _pipeline(cfg, store).budget().remaining_minutes == 2700


def test_daily_circuit_breaker_binds_when_tighter(store):
    cfg = Config()
    cfg.limits.daily_minutes = 60
    _record(store, "a", minutes=30)
    pipeline = _pipeline(cfg, store)

    budgets = pipeline.budgets()
    assert [b.period for b in budgets] == ["month", "24h"]
    # The tightest one is what actually gates work.
    binding = pipeline.budget()
    assert binding.period == "24h"
    assert binding.remaining_minutes == 30


def test_rolling_daily_window_forgets_yesterday(store):
    cfg = Config()
    cfg.limits.daily_minutes = 60
    _record(store, "old", minutes=60, when=_now() - timedelta(hours=25))
    daily = [b for b in _pipeline(cfg, store).budgets() if b.period == "24h"][0]
    assert daily.remaining_minutes == 60


def test_zero_disables_every_limit(store):
    cfg = Config()
    cfg.limits.monthly_minutes = 0
    cfg.limits.daily_minutes = 0
    _record(store, "a", minutes=9000)
    assert _pipeline(cfg, store).budget() is None


def test_oversized_recording_is_skipped_not_failed(store, monkeypatch, tmp_path):
    from plaud_scribe import sync

    monkeypatch.setattr(sync, "RAW_DIR", tmp_path)
    cfg = Config()
    _record(store, "spent", minutes=2680)
    pipeline = _pipeline(cfg, store)

    def never(*args, **kwargs):
        raise AssertionError("transcribe must not be called when over budget")

    monkeypatch.setattr(pipeline, "transcribe", never)
    result = pipeline.process({"id": "big", "name": "Long call", "duration": 60 * 60_000})

    assert result.status == "skipped"
    assert result.counts_as_failure is False
    assert "20 min left of 2700 min per month" in result.error
    # Left pending so the next run retries it.
    assert store.get("big").status == "pending"


def test_within_budget_proceeds_to_transcription(store, monkeypatch, tmp_path):
    from plaud_scribe import sync

    monkeypatch.setattr(sync, "RAW_DIR", tmp_path)
    called = []

    def record_then_stop(item):
        called.append(item)
        raise RuntimeError("stop here")

    pipeline = _pipeline(Config(), store)
    monkeypatch.setattr(pipeline, "transcribe", record_then_stop)
    result = pipeline.process({"id": "ok", "name": "Short", "duration": 10 * 60_000})

    assert called, "a 10-minute recording fits in the 2700-minute budget"
    assert result.status == "failed"  # the deliberate error, raised past the budget gate


def test_cached_transcript_is_free(store, monkeypatch, tmp_path):
    """Re-rendering must not consume budget: no audio is sent."""
    from plaud_scribe import sync

    monkeypatch.setattr(sync, "RAW_DIR", tmp_path)
    cfg = Config()
    _record(store, "spent", minutes=2700)
    pipeline = _pipeline(cfg, store)
    assert pipeline.budget().remaining_minutes == 0

    cache_path("cached").write_text('{"version": 1, "transcript": {"words": []}}')

    def never(*args, **kwargs):
        raise AssertionError("cached recordings must not be re-transcribed")

    monkeypatch.setattr(pipeline, "transcribe", never)
    result = pipeline.process(
        {"id": "cached", "name": "Cached", "duration": 60 * 60_000}, upload=False
    )
    assert result.status != "skipped"
