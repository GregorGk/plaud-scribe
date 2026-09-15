"""Turn diarization IDs into human names.

Two mechanisms, in order of trust:
  1. An alias map from config — you fill it in once per device/meeting and it is exact.
  2. An optional Claude pass that reads the transcript and picks up names only where
     someone is introduced or addressed by name. Off unless enabled in config.
"""

from __future__ import annotations

import json
import logging
import re

from .config import Config
from .stt.base import Turn, default_speaker_label

log = logging.getLogger(__name__)

# How much of the transcript to show the naming model.
NAMING_CHAR_BUDGET = 12_000

NAMING_SYSTEM = """You label speakers in a multilingual meeting transcript.

The transcript may mix English, German, Polish, Portuguese and Russian. Speakers are
identified only by an opaque id such as speaker_0.

Return a JSON object mapping speaker id to the person's name, and nothing else. Include a
speaker ONLY when the transcript itself makes the name clear — someone introduces
themselves, or another speaker addresses them by name and the reply confirms it. Omit
every speaker you cannot identify that way. Never invent a name, and never guess from
accent, language or topic. If no speaker can be identified, return {}."""


def _normalise_key(key: str) -> str:
    """Accept `speaker_0`, `0`, `Speaker 1` and `speaker 1` as aliases for one id."""
    text = key.strip().lower()
    if text.startswith("speaker"):
        digits = re.sub(r"[^0-9]", "", text)
        if digits:
            # `Speaker 1` in config means the first speaker, i.e. speaker_0.
            if text.startswith("speaker_"):
                return f"speaker_{int(digits)}"
            return f"speaker_{int(digits) - 1}"
    if text.isdigit():
        return f"speaker_{int(text)}"
    return text


def resolve_aliases(cfg: Config, recording_id: str) -> dict[str, str]:
    merged = dict(cfg.speakers.aliases)
    merged.update(cfg.speakers.per_recording.get(recording_id, {}))
    return {_normalise_key(k): v for k, v in merged.items()}


def apply_names(turns: list[Turn], names: dict[str, str]) -> None:
    for turn in turns:
        name = names.get(turn.speaker_id)
        if name:
            turn.speaker_name = name


def infer_names(turns: list[Turn], cfg: Config) -> dict[str, str]:
    """Ask Claude which speakers the transcript itself identifies. Best effort."""
    try:
        import anthropic
    except ImportError:
        log.warning("speakers.llm_naming is on but the `anthropic` package is not installed")
        return {}

    excerpt: list[str] = []
    used = 0
    for turn in turns:
        line = f"[{turn.speaker_id}] {turn.text}"
        if used + len(line) > NAMING_CHAR_BUDGET:
            break
        excerpt.append(line)
        used += len(line)
    if not excerpt:
        return {}

    from .summary import claude_client

    try:
        client = claude_client(cfg)
        response = client.messages.create(
            model=cfg.speakers.llm_model,
            max_tokens=1024,
            system=NAMING_SYSTEM,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": "\n".join(excerpt)}],
        )
    except Exception as err:  # naming is a nicety; never fail a sync over it
        log.warning("Speaker naming failed: %s", err)
        return {}

    text = "".join(block.text for block in response.content if block.type == "text")
    return _parse_mapping(text, {turn.speaker_id for turn in turns})


def _parse_mapping(text: str, known_ids: set[str]) -> dict[str, str]:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    result = {}
    for key, value in data.items():
        speaker = _normalise_key(str(key))
        if speaker in known_ids and isinstance(value, str) and value.strip():
            result[speaker] = value.strip()
    return result


def summarise(turns: list[Turn]) -> list[str]:
    """Display labels in first-appearance order, for the front matter."""
    seen: dict[str, str] = {}
    for turn in turns:
        seen.setdefault(turn.speaker_id, turn.speaker_name or default_speaker_label(turn.speaker_id))
    return list(seen.values())
