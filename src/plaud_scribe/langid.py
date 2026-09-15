"""Offline per-turn language identification.

Scribe v2 returns one language code for the whole file, but these recordings switch
language mid-conversation. Tagging each turn locally with lingua costs nothing and is
restricted to the languages actually expected, which keeps short turns from being
assigned to something exotic.
"""

from __future__ import annotations

from collections import Counter
from functools import lru_cache

from .stt.base import Turn

MIN_CONFIDENCE = 0.60


@lru_cache(maxsize=4)
def _detector(codes: tuple[str, ...]):
    from lingua import IsoCode639_1, LanguageDetectorBuilder

    iso = []
    for code in codes:
        member = getattr(IsoCode639_1, code.upper(), None)
        if member is None:
            raise ValueError(f"Unsupported language code in config: {code}")
        iso.append(member)
    if len(iso) < 2:
        raise ValueError("Language detection needs at least two expected languages")
    return LanguageDetectorBuilder.from_iso_codes_639_1(*iso).build()


class LanguageTagger:
    def __init__(self, expected: list[str], *, min_chars: int = 12) -> None:
        self.codes = tuple(dict.fromkeys(c.lower() for c in expected))
        self.min_chars = min_chars

    def detect(self, text: str) -> str | None:
        """Return an ISO-639-1 code, or None when the text is too short to trust."""
        stripped = text.strip()
        if len(stripped) < self.min_chars:
            return None
        values = _detector(self.codes).compute_language_confidence_values(stripped)
        if not values:
            return None
        best = values[0]
        if best.value < MIN_CONFIDENCE:
            return None
        return best.language.iso_code_639_1.name.lower()

    def tag(self, turns: list[Turn]) -> tuple[str | None, list[str]]:
        """Annotate every turn in place.

        Returns (majority language, all languages present ordered by spoken share).
        """
        weights: Counter[str] = Counter()
        for turn in turns:
            language = self.detect(turn.text)
            turn.language = language
            if language:
                weights[language] += len(turn.text)
        if not weights:
            return None, []
        ordered = [code for code, _ in weights.most_common()]
        return ordered[0], ordered
