"""Group word-level output into speaker turns and into subtitle cues."""

from __future__ import annotations

import re

from .stt.base import Turn, Word

SENTENCE_END = re.compile(r"[.!?…]['\")\]]?$")
WHITESPACE = re.compile(r"\s+")


def _text_of(words: list[Word]) -> str:
    """Join a run of words, inserting the space the provider left implicit.

    Scribe normally emits explicit `spacing` entries, but audio events (and the odd
    boundary) can arrive without one — without this, tags glue onto the previous word.
    """
    parts: list[str] = []
    previous_was_text = False
    for word in words:
        if word.is_spacing:
            parts.append(word.text)
            previous_was_text = False
            continue
        if previous_was_text:
            parts.append(" ")
        parts.append(word.text)
        previous_was_text = True
    return WHITESPACE.sub(" ", "".join(parts)).strip()


def _ends_sentence(words: list[Word]) -> bool:
    for word in reversed(words):
        if word.is_spacing:
            continue
        return bool(SENTENCE_END.search(word.text.strip()))
    return False


def build_turns(
    words: list[Word],
    *,
    gap_seconds: float = 2.0,
    max_seconds: float = 60.0,
    max_chars: int | None = None,
    strict_length: bool = False,
) -> list[Turn]:
    """Break on speaker change, on a silence longer than `gap_seconds`, and on length.

    For prose, `strict_length` is False: a length break waits for a sentence boundary so a
    turn is not cut mid-clause, and a monologue that never punctuates is force-split at
    twice the limit. Subtitle cues set `strict_length` and `max_chars` instead, where
    fitting the box matters more than the clause.
    """
    turns: list[Turn] = []
    buffer: list[Word] = []
    speaker: str | None = None
    start: float | None = None
    end: float | None = None
    chars = 0

    def flush() -> None:
        nonlocal buffer, speaker, start, end, chars
        text = _text_of(buffer)
        if text and speaker is not None:
            turns.append(
                Turn(
                    speaker_id=speaker,
                    start=start if start is not None else 0.0,
                    end=end if end is not None else (start or 0.0),
                    text=text,
                )
            )
        buffer = []
        speaker = None
        start = None
        end = None
        chars = 0

    for word in words:
        if word.is_spacing:
            if buffer:
                buffer.append(word)
                chars += len(word.text)
            continue

        word_speaker = word.speaker_id or speaker or "speaker_0"
        word_start = word.start if word.start is not None else end
        word_end = word.end if word.end is not None else word_start

        if buffer:
            gap = word_start - end if word_start is not None and end is not None else 0.0
            span = word_end - start if word_end is not None and start is not None else 0.0
            if strict_length:
                too_long = span > max_seconds or (
                    max_chars is not None and chars + len(word.text) > max_chars
                )
            else:
                too_long = (span > max_seconds and _ends_sentence(buffer)) or (
                    span > max_seconds * 2
                )
            if word_speaker != speaker or gap > gap_seconds or too_long:
                flush()

        if not buffer:
            speaker = word_speaker
            start = word_start
        buffer.append(word)
        chars += len(word.text)
        if word_end is not None:
            end = word_end

    flush()
    return turns


def build_cues(
    words: list[Word],
    *,
    max_chars: int = 74,
    max_seconds: float = 6.0,
    gap_seconds: float = 1.0,
) -> list[Turn]:
    """Subtitle-sized fragments: one speaker, short enough to read."""
    return build_turns(
        words,
        gap_seconds=gap_seconds,
        max_seconds=max_seconds,
        max_chars=max_chars,
        strict_length=True,
    )
