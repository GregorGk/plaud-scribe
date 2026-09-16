"""Render turns into the artefacts that go to Drive."""

from __future__ import annotations

import json
import textwrap
from dataclasses import dataclass
from datetime import datetime

from .stt.base import Turn

SUBTITLE_LINE_WIDTH = 42


def quote(value: str) -> str:
    """Double-quoted scalar for the front matter, with diacritics left intact.

    json.dumps escapes non-ASCII by default, which turns "nieruchomości" into
    "nieruchomo\\u015bci". JSON string syntax is a subset of YAML's, so the quoting
    rules still hold with ensure_ascii off.
    """
    return json.dumps(value, ensure_ascii=False)


@dataclass
class RecordingMeta:
    recording_id: str
    title: str
    recorded_at: datetime | None
    duration_seconds: float
    provider: str
    model_id: str
    transcribed_at: datetime | None = None


def format_clock(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def format_duration(seconds: float) -> str:
    total = int(seconds)
    hours, minutes, secs = total // 3600, total % 3600 // 60, total % 60
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def _stamp(seconds: float, separator: str) -> str:
    if seconds < 0:
        seconds = 0.0
    total = int(seconds)
    millis = int(round((seconds - total) * 1000))
    if millis == 1000:  # rounding carried into the next second
        total += 1
        millis = 0
    return (
        f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}{separator}{millis:03d}"
    )


def render_markdown(
    turns: list[Turn],
    meta: RecordingMeta,
    *,
    majority_language: str | None = None,
    languages: list[str] | None = None,
    speakers: list[str] | None = None,
) -> str:
    front: list[str] = ["---"]
    front.append(f"title: {quote(meta.title)}")
    if meta.recorded_at:
        front.append(f"recorded: {meta.recorded_at.isoformat()}")
    front.append(f"duration: {format_duration(meta.duration_seconds)}")
    if languages:
        front.append(f"languages: [{', '.join(languages)}]")
    if speakers:
        front.append(f"speakers: [{', '.join(quote(s) for s in speakers)}]")
    front.append(f"plaud_id: {meta.recording_id}")
    front.append(f"model: {meta.provider}/{meta.model_id}")
    if meta.transcribed_at:
        front.append(f"transcribed: {meta.transcribed_at.isoformat()}")
    front.append("---")

    body: list[str] = ["", f"# {meta.title}", ""]
    for turn in turns:
        prefix = f"**[{format_clock(turn.start)}] {turn.label}:**"
        # Only flag a language when the turn departs from the recording's main one.
        if turn.language and majority_language and turn.language != majority_language:
            prefix = f"{prefix} _({turn.language})_"
        body.append(f"{prefix} {turn.text}")
        body.append("")
    return "\n".join(front + body).rstrip() + "\n"


def render_text(
    turns: list[Turn],
    meta: RecordingMeta,
    *,
    majority_language: str | None = None,
    speakers: list[str] | None = None,
) -> str:
    """Plain text: a two-line header, then one line per turn. No markup."""
    facts = []
    if meta.recorded_at:
        facts.append("Recorded " + meta.recorded_at.strftime("%Y-%m-%d %H:%M %Z").strip())
    facts.append("Duration " + format_duration(meta.duration_seconds))
    if speakers:
        facts.append("Speakers: " + ", ".join(speakers))
    lines = [meta.title, " | ".join(facts), ""]
    for turn in turns:
        tag = ""
        if turn.language and majority_language and turn.language != majority_language:
            tag = f" ({turn.language})"
        lines.append(f"[{format_clock(turn.start)}] {turn.label}{tag}: {turn.text}")
    return "\n".join(lines).rstrip() + "\n"


def transcript_for_llm(turns: list[Turn]) -> str:
    """What the summariser reads: timestamped, speaker-labelled lines."""
    return "\n".join(f"[{format_clock(t.start)}] {t.label}: {t.text}" for t in turns)


def _subtitle_text(turn: Turn) -> str:
    """Speaker-prefixed cue text, wrapped. Never drops words to fit."""
    line = f"{turn.label}: {turn.text}".strip()
    return "\n".join(textwrap.wrap(line, width=SUBTITLE_LINE_WIDTH) or [line])


def render_srt(cues: list[Turn]) -> str:
    blocks = []
    for index, cue in enumerate(cues, start=1):
        blocks.append(
            f"{index}\n"
            f"{_stamp(cue.start, ',')} --> {_stamp(max(cue.end, cue.start + 0.5), ',')}\n"
            f"{_subtitle_text(cue)}\n"
        )
    return "\n".join(blocks)


def render_vtt(cues: list[Turn]) -> str:
    blocks = ["WEBVTT\n"]
    for cue in cues:
        blocks.append(
            f"{_stamp(cue.start, '.')} --> {_stamp(max(cue.end, cue.start + 0.5), '.')}\n"
            f"{_subtitle_text(cue)}\n"
        )
    return "\n".join(blocks)
