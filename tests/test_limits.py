"""The rolling 24-hour transcription budget."""

from datetime import datetime, timedelta, timezone

import pytest

from plaud_scribe.config import Config
from plaud_scribe.state import Store
from plaud_scribe.sync import Pipeline, cache_path


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "state.db") as store:
        yield store


def _record(store, rid, *, minutes, hours_ago):
    when = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    store.note_seen(rid, rid, when.isoformat(), minutes * 60)
    store.mark_done(
        rid,
        duration_seconds=minutes * 60,
        provider="elevenlabs",
        model_id="scribe_v2",
        cost_usd=0.1,
        transcribed_at=when.isoformat(timespec="seconds"),
    )


def _pipeline(cfg, store):
    return Pipeline(cfg, plaud=None, store=store)


def test_budget_starts_full_and_depletes(store):
    cfg = Config()
    pipeline = _pipeline(cfg, store)
    assert pipeline.remaining_budget_seconds() == 90 * 60

    _record(store, "a", minutes=30, hours_ago=1)
    assert pipeline.remaining_budget_seconds() == 60 * 60

    _record(store, "b", minutes=70, hours_ago=2)
    # Clamped at zero rather than going negative.
    assert pipeline.remaining_budget_seconds() == 0


def test_window_is_rolling_not_calendar(store):
    cfg = Config()
    _record(store, "old", minutes=90, hours_ago=25)
    assert _pipeline(cfg, store).remaining_budget_seconds() == 90 * 60


def test_zero_disables_the_limit(store):
    cfg = Config()
    cfg.limits.daily_minutes = 0
    _record(store, "a", minutes=500, hours_ago=1)
    assert _pipeline(cfg, store).remaining_budget_seconds() is None


def test_oversized_recording_is_skipped_not_failed(store, monkeypatch, tmp_path):
    from plaud_scribe import sync

    monkeypatch.setattr(sync, "RAW_DIR", tmp_path)
    cfg = Config()
    pipeline = _pipeline(cfg, store)

    def never(*args, **kwargs):
        raise AssertionError("transcribe must not be called when over budget")

    monkeypatch.setattr(pipeline, "transcribe", never)

    result = pipeline.process({"id": "big", "name": "Long call", "duration": 120 * 60_000})

    assert result.status == "skipped"
    assert result.counts_as_failure is False
    assert "daily limit" in result.error
    # Left pending so the next run retries it.
    assert store.get("big").status == "pending"


def test_within_budget_proceeds_to_transcription(store, monkeypatch, tmp_path):
    from plaud_scribe import sync

    monkeypatch.setattr(sync, "RAW_DIR", tmp_path)
    cfg = Config()
    pipeline = _pipeline(cfg, store)
    called = []
    monkeypatch.setattr(pipeline, "transcribe", lambda item: called.append(item) or (_ for _ in ()).throw(RuntimeError("stop here")))

    result = pipeline.process({"id": "ok", "name": "Short", "duration": 10 * 60_000})

    assert called, "a 10-minute recording fits in the 90-minute budget"
    assert result.status == "failed"  # our deliberate RuntimeError, past the budget gate


def test_cached_transcript_is_free(store, monkeypatch, tmp_path):
    """Re-rendering must not consume budget: no transcription happens."""
    from plaud_scribe import sync

    monkeypatch.setattr(sync, "RAW_DIR", tmp_path)
    cfg = Config()
    _record(store, "spent", minutes=90, hours_ago=1)
    pipeline = _pipeline(cfg, store)
    assert pipeline.remaining_budget_seconds() == 0

    cache_path("cached").write_text('{"version": 1, "transcript": {"words": []}}')

    def never(*args, **kwargs):
        raise AssertionError("cached recordings must not be re-transcribed")

    monkeypatch.setattr(pipeline, "transcribe", never)
    result = pipeline.process(
        {"id": "cached", "name": "Cached", "duration": 60 * 60_000}, upload=False
    )
    assert result.status != "skipped"
