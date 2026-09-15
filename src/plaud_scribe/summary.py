"""Meeting summary written by Claude, cached next to the raw transcript.

The summary costs a model call, so it is generated once per transcript and re-used by
`render`. Re-transcribing (or `render --resummarize`) invalidates it. A failed summary
never fails the recording: the transcript still lands in Drive and the summary is retried
on the next `render --upload` or `sync --retry-failed`.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import RAW_DIR, Config
from .render import RecordingMeta, format_duration, transcript_for_llm
from .stt.base import Turn

log = logging.getLogger(__name__)

CACHE_VERSION = 1

SYSTEM_PROMPT = """You write summaries of recorded conversations for the person who made
the recording. The transcript comes from a voice recorder: it may be a meeting, a call, a
lecture, a voice memo or an informal chat, and it may switch between languages mid-way.
Speakers are labelled by diarization ("Speaker 1", or a real name when known); labels can
occasionally be wrong, so do not over-interpret who said what.

Write in Markdown, using exactly these sections and nothing else:

## Overview
Two to five sentences: what this recording is, who takes part, and what it is about.

## Key points
The substance, as bullets. Group by topic when the conversation covers several. Keep
figures, names, dates and commitments exactly as stated.

## Decisions
Bullets. Write "None recorded." if there are none.

## Action items
Bullets in the form "- **Who**: what, by when (if said)". Write "None recorded." if there
are none.

## Open questions
Anything left unresolved or explicitly deferred. Write "None." if there are none.

Rules: use only what is in the transcript; never invent details or fill gaps. Do not
mention that you are summarising a transcript. Do not add a title line."""

LANGUAGE_RULE_AUTO = (
    "Write the summary in the language that dominates the transcript. Keep quoted terms, "
    "names and jargon in their original language."
)
LANGUAGE_RULE_FIXED = (
    "Write the summary in {language}, regardless of the transcript's language(s). Keep "
    "names and jargon in their original form."
)

SERVER_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class SummaryError(RuntimeError):
    pass


@dataclass
class Summary:
    text: str
    model: str
    generated_at: datetime
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    served_by: str | None = None


def cache_path(recording_id: str) -> Path:
    return RAW_DIR / f"{recording_id}.summary.json"


def transcript_hash(turns: list[Turn]) -> str:
    return hashlib.sha256(transcript_for_llm(turns).encode("utf-8")).hexdigest()[:16]


def read_cache(recording_id: str, turns: list[Turn]) -> Summary | None:
    path = cache_path(recording_id)
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if data.get("version") != CACHE_VERSION or data.get("transcript_hash") != transcript_hash(turns):
        return None
    try:
        return Summary(
            text=data["text"],
            model=data["model"],
            generated_at=datetime.fromisoformat(data["generated_at"]),
            input_tokens=int(data.get("input_tokens", 0)),
            output_tokens=int(data.get("output_tokens", 0)),
            cost_usd=float(data.get("cost_usd", 0.0)),
            served_by=data.get("served_by"),
        )
    except (KeyError, ValueError):
        return None


def write_cache(recording_id: str, turns: list[Turn], summary: Summary) -> Path:
    path = cache_path(recording_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": CACHE_VERSION,
        "transcript_hash": transcript_hash(turns),
        "model": summary.model,
        "served_by": summary.served_by,
        "generated_at": summary.generated_at.isoformat(timespec="seconds"),
        "input_tokens": summary.input_tokens,
        "output_tokens": summary.output_tokens,
        "cost_usd": summary.cost_usd,
        "text": summary.text,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    return path


def claude_client(cfg: Config):
    """One place that builds the Anthropic client, shared with speaker naming."""
    import anthropic

    return anthropic.Anthropic(api_key=cfg.require_anthropic_key(), max_retries=3)


def build_prompt(turns: list[Turn], meta: RecordingMeta, cfg: Config) -> tuple[str, str]:
    language = cfg.summary.language.strip().lower()
    rule = LANGUAGE_RULE_AUTO if language in ("", "auto") else LANGUAGE_RULE_FIXED.format(language=cfg.summary.language)
    system = f"{SYSTEM_PROMPT}\n\n{rule}"
    facts = [f"Title: {meta.title}"]
    if meta.recorded_at:
        facts.append(f"Recorded: {meta.recorded_at.isoformat(timespec='minutes')}")
    facts.append(f"Duration: {format_duration(meta.duration_seconds)}")
    user = "\n".join(facts) + "\n\n<transcript>\n" + transcript_for_llm(turns) + "\n</transcript>"
    return system, user


def generate(turns: list[Turn], meta: RecordingMeta, cfg: Config) -> Summary:
    """Call Claude. Raises SummaryError on anything that should be surfaced."""
    import anthropic

    if not any(t.text.strip() for t in turns):
        raise SummaryError("transcript is empty; nothing to summarise")

    client = claude_client(cfg)
    system, user = build_prompt(turns, meta, cfg)
    request = {
        "model": cfg.summary.model,
        "max_tokens": cfg.summary.max_output_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": cfg.summary.effort},
    }

    try:
        response = _run(client, request, fallbacks=cfg.summary.fallbacks)
    except anthropic.BadRequestError as err:
        if cfg.summary.fallbacks and "fallback" in str(err).lower():
            log.warning("Server-side fallbacks not accepted (%s); retrying without", err)
            response = _run(client, request, fallbacks=False)
        else:
            raise SummaryError(f"Claude rejected the request: {err}") from err
    except anthropic.AuthenticationError as err:
        raise SummaryError("Anthropic API key was rejected") from err
    except anthropic.APIError as err:
        raise SummaryError(f"Claude API error: {err}") from err

    if response.stop_reason == "refusal":
        details = getattr(response, "stop_details", None)
        why = getattr(details, "explanation", None) or getattr(details, "category", None) or "no reason given"
        raise SummaryError(f"Claude declined to summarise this recording ({why})")
    if response.stop_reason == "max_tokens":
        log.warning("Summary hit max_output_tokens=%s; it may be cut short", cfg.summary.max_output_tokens)

    text = "".join(block.text for block in response.content if block.type == "text").strip()
    if not text:
        raise SummaryError("Claude returned no text")

    usage = response.usage
    cost = (
        usage.input_tokens * cfg.summary.input_price_per_mtok
        + usage.output_tokens * cfg.summary.output_price_per_mtok
    ) / 1_000_000
    served_by = getattr(response, "model", None)
    return Summary(
        text=text,
        model=cfg.summary.model,
        generated_at=datetime.now(timezone.utc),
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cost_usd=cost,
        served_by=served_by if served_by != cfg.summary.model else None,
    )


def _run(client, request: dict, *, fallbacks: bool):
    # Streaming keeps long transcripts clear of HTTP timeouts.
    if fallbacks:
        with client.beta.messages.stream(
            **request, betas=[SERVER_FALLBACK_BETA], fallbacks="default"
        ) as stream:
            return stream.get_final_message()
    with client.messages.stream(**request) as stream:
        return stream.get_final_message()


def render_document(summary: Summary, meta: RecordingMeta) -> str:
    front = ["---", f"title: {json.dumps(meta.title)}"]
    if meta.recorded_at:
        front.append(f"recorded: {meta.recorded_at.isoformat()}")
    front.append(f"duration: {format_duration(meta.duration_seconds)}")
    front.append(f"plaud_id: {meta.recording_id}")
    front.append(f"summary_model: {summary.served_by or summary.model}")
    front.append(f"summarised: {summary.generated_at.isoformat(timespec='seconds')}")
    front.append("---")
    return "\n".join(front) + f"\n\n# {meta.title} — summary\n\n{summary.text.strip()}\n"


def obtain(
    recording_id: str,
    turns: list[Turn],
    meta: RecordingMeta,
    cfg: Config,
    *,
    force: bool = False,
) -> Summary:
    """Cached summary if the transcript is unchanged, otherwise a fresh one."""
    if not force:
        cached = read_cache(recording_id, turns)
        if cached is not None:
            return cached
    summary = generate(turns, meta, cfg)
    write_cache(recording_id, turns, summary)
    return summary
