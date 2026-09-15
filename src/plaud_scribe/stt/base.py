"""Provider-neutral transcript model.

Everything downstream of transcription (segmentation, rendering, language tagging)
works on these types, so a second engine only has to produce a `Transcript`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class Word:
    text: str
    start: float | None = None
    end: float | None = None
    speaker_id: str | None = None
    type: str = "word"

    @property
    def is_spacing(self) -> bool:
        return self.type == "spacing"


@dataclass
class Transcript:
    text: str
    language_code: str
    words: list[Word]
    provider: str
    model_id: str
    language_probability: float | None = None
    duration_seconds: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_raw(cls, raw: dict[str, Any], provider: str, model_id: str) -> "Transcript":
        """Rebuild from the cached provider payload — used by `render` and by tests."""
        words = [
            Word(
                text=w.get("text", ""),
                start=w.get("start"),
                end=w.get("end"),
                speaker_id=w.get("speaker_id"),
                type=w.get("type", "word"),
            )
            for w in raw.get("words", [])
        ]
        return cls(
            text=raw.get("text", ""),
            language_code=raw.get("language_code", ""),
            words=words,
            provider=provider,
            model_id=model_id,
            language_probability=raw.get("language_probability"),
            duration_seconds=raw.get("duration_seconds"),
            raw=raw,
        )


@dataclass
class Turn:
    """One uninterrupted stretch of speech by a single speaker."""

    speaker_id: str
    start: float
    end: float
    text: str
    language: str | None = None
    speaker_name: str | None = None

    @property
    def label(self) -> str:
        return self.speaker_name or default_speaker_label(self.speaker_id)


def default_speaker_label(speaker_id: str) -> str:
    """`speaker_0` -> `Speaker 1`; anything else passes through unchanged."""
    if speaker_id.startswith("speaker_"):
        suffix = speaker_id.removeprefix("speaker_")
        if suffix.isdigit():
            return f"Speaker {int(suffix) + 1}"
    return speaker_id


class SttProvider(Protocol):
    name: str

    def transcribe(
        self,
        *,
        source_url: str | None = None,
        file_path: str | None = None,
        num_speakers: int | None = None,
    ) -> Transcript: ...

    def estimate_cost(self, duration_seconds: float) -> float: ...
