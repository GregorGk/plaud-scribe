"""Summary generation and caching, with the Anthropic client faked."""

from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest

from plaud_scribe import summary
from plaud_scribe.config import Config
from plaud_scribe.render import RecordingMeta
from plaud_scribe.segment import build_turns


@dataclass
class _Usage:
    input_tokens: int = 1200
    output_tokens: int = 300


@dataclass
class _Block:
    type: str
    text: str = ""


@dataclass
class _Response:
    content: list = field(default_factory=list)
    stop_reason: str = "end_turn"
    usage: _Usage = field(default_factory=_Usage)
    model: str = "claude-sonnet-5"
    stop_details: object = None


class _Stream:
    def __init__(self, response):
        self._response = response

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._response


class _Messages:
    def __init__(self, response, calls):
        self._response = response
        self._calls = calls

    def stream(self, **kwargs):
        self._calls.append(kwargs)
        if isinstance(self._response, Exception):
            raise self._response
        return _Stream(self._response)


class _FakeClient:
    def __init__(self, response):
        self.calls: list[dict] = []
        self.messages = _Messages(response, self.calls)
        self.beta = type("Beta", (), {"messages": _Messages(response, self.calls)})()


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(summary, "RAW_DIR", tmp_path)
    cfg = Config()
    cfg.summary.api_key = "sk-test"
    return cfg


@pytest.fixture
def meta(envelope):
    return RecordingMeta(
        recording_id="rec_test_0001",
        title=envelope["recording"]["name"],
        recorded_at=datetime(2026, 8, 24, 14, 3, tzinfo=timezone.utc),
        duration_seconds=62.5,
        provider="elevenlabs",
        model_id="scribe_v2",
    )


def test_prompt_carries_transcript_and_language_rule(transcript, meta, cfg):
    turns = build_turns(transcript.words)
    system, user = summary.build_prompt(turns, meta, cfg)
    assert "## Action items" in system
    assert "dominates the transcript" in system
    assert "<transcript>" in user and "[00:00:04] Speaker 1:" in user
    assert "Title: Weekly sync / Wochentreffen" in user

    cfg.summary.language = "en"
    system, _ = summary.build_prompt(turns, meta, cfg)
    assert "Write the summary in en" in system


def test_generate_uses_fallbacks_and_prices_usage(transcript, meta, cfg, monkeypatch):
    fake = _FakeClient(_Response(content=[_Block("thinking"), _Block("text", "## Overview\nHi.")]))
    monkeypatch.setattr(summary, "claude_client", lambda cfg: fake)
    turns = build_turns(transcript.words)

    result = summary.generate(turns, meta, cfg)

    assert result.text == "## Overview\nHi."
    assert result.served_by is None
    # Sonnet 5 rates, taken from the model, not a hard-coded figure.
    assert result.cost_usd == pytest.approx((1200 * 2.0 + 300 * 10.0) / 1e6)
    call = fake.calls[0]
    assert call["model"] == "claude-sonnet-5"
    assert call["fallbacks"] == "default"
    assert call["betas"] == [summary.SERVER_FALLBACK_BETA]
    assert call["thinking"] == {"type": "adaptive"}
    assert call["output_config"] == {"effort": "medium"}


def test_generate_without_fallbacks_uses_plain_endpoint(transcript, meta, cfg, monkeypatch):
    fake = _FakeClient(_Response(content=[_Block("text", "ok")], model="claude-opus-4-8"))
    monkeypatch.setattr(summary, "claude_client", lambda cfg: fake)
    cfg.summary.fallbacks = False

    result = summary.generate(build_turns(transcript.words), meta, cfg)

    assert "fallbacks" not in fake.calls[0]
    assert result.served_by == "claude-opus-4-8"


def test_refusal_is_an_error(transcript, meta, cfg, monkeypatch):
    details = type("Details", (), {"category": "cyber", "explanation": None})()
    fake = _FakeClient(_Response(content=[], stop_reason="refusal", stop_details=details))
    monkeypatch.setattr(summary, "claude_client", lambda cfg: fake)
    with pytest.raises(summary.SummaryError, match="declined.*cyber"):
        summary.generate(build_turns(transcript.words), meta, cfg)


def test_cache_round_trip_and_invalidation(transcript, meta, cfg, monkeypatch):
    calls = []

    def fake_generate(turns, meta, cfg):
        calls.append(1)
        return summary.Summary(
            text="## Overview\ncached", model="claude-sonnet-5",
            generated_at=datetime(2026, 9, 1, tzinfo=timezone.utc), cost_usd=0.01,
        )

    monkeypatch.setattr(summary, "generate", fake_generate)
    turns = build_turns(transcript.words)

    first = summary.obtain("rec_test_0001", turns, meta, cfg)
    second = summary.obtain("rec_test_0001", turns, meta, cfg)
    assert len(calls) == 1 and second.text == first.text
    assert summary.cache_path("rec_test_0001").exists()

    summary.obtain("rec_test_0001", turns, meta, cfg, force=True)
    assert len(calls) == 2

    turns[0].text = "something else was said"
    summary.obtain("rec_test_0001", turns, meta, cfg)
    assert len(calls) == 3


def test_price_follows_the_model(cfg):
    from plaud_scribe.config import MODEL_PRICES_PER_MTOK

    cfg.summary.model = "claude-sonnet-5"
    assert cfg.summary.prices() == MODEL_PRICES_PER_MTOK["claude-sonnet-5"]

    cfg.summary.model = "claude-opus-5"
    assert cfg.summary.prices() == MODEL_PRICES_PER_MTOK["claude-opus-5"]

    # An explicit override always wins.
    cfg.summary.input_price_per_mtok = 7.5
    cfg.summary.output_price_per_mtok = 30.0
    assert cfg.summary.prices() == (7.5, 30.0)


def test_unknown_model_over_estimates_rather_than_under(cfg, caplog):
    cfg.summary.model = "some-future-model"
    with caplog.at_level("WARNING"):
        prices = cfg.summary.prices()
    assert prices == (10.0, 50.0)  # dearest known, so spend is never under-reported
    assert "No price known" in caplog.text


def test_document_has_front_matter_and_heading(meta):
    result = summary.Summary(
        text="## Overview\nShort.", model="claude-sonnet-5",
        generated_at=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
    )
    doc = summary.render_document(result, meta)
    assert doc.startswith("---\n")
    assert "plaud_id: rec_test_0001" in doc
    assert "summary_model: claude-sonnet-5" in doc
    assert "# Weekly sync / Wochentreffen — summary" in doc
    assert doc.rstrip().endswith("Short.")


def test_pipeline_keeps_transcript_when_summary_fails(transcript, envelope, cfg, monkeypatch, tmp_path):
    from plaud_scribe import sync
    from plaud_scribe.plaud import parse_timestamp

    def boom(*args, **kwargs):
        raise summary.SummaryError("no key")

    monkeypatch.setattr(summary, "obtain", boom)
    pipeline = sync.Pipeline(cfg, plaud=None, store=None)
    rendered = pipeline.build_documents(
        recording_id="rec_test_0001",
        title=envelope["recording"]["name"],
        recorded_at=parse_timestamp(envelope["recording"]["start_at"]),
        duration=62.5,
        transcript=transcript,
    )
    assert "md" in rendered.documents and "txt" in rendered.documents
    assert "summary" not in rendered.documents
    assert rendered.warnings == ["summary skipped: no key"]
