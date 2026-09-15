"""ElevenLabs Scribe v2 adapter.

Language is deliberately left unset: Scribe v2 detects and code-switches within a single
file, which is the whole point for recordings that mix English, German, Polish,
Portuguese and Russian.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx
from elevenlabs.client import ElevenLabs
from elevenlabs.core.api_error import ApiError

from ..config import Config
from .base import Transcript

log = logging.getLogger(__name__)

RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504}
MAX_ATTEMPTS = 4


class TranscriptionError(RuntimeError):
    pass


class ElevenLabsProvider:
    name = "elevenlabs"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.settings = cfg.elevenlabs
        self.model_id = self.settings.model_id
        self._client = ElevenLabs(
            api_key=cfg.require_elevenlabs_key(),
            timeout=self.settings.timeout_seconds,
        )

    def estimate_cost(self, duration_seconds: float) -> float:
        return duration_seconds / 3600 * self.settings.hourly_rate()

    def transcribe(
        self,
        *,
        source_url: str | None = None,
        file_path: str | None = None,
        num_speakers: int | None = None,
    ) -> Transcript:
        if bool(source_url) == bool(file_path):
            raise ValueError("Pass exactly one of source_url or file_path")

        kwargs: dict[str, Any] = {
            "model_id": self.model_id,
            "diarize": True,
            "tag_audio_events": self.settings.tag_audio_events,
            "timestamps_granularity": "word",
            # language_code stays unset so the model handles code-switching itself.
        }
        speakers = num_speakers if num_speakers is not None else self.settings.num_speakers
        if speakers:
            kwargs["num_speakers"] = speakers
        elif self.settings.diarization_threshold is not None:
            # Only accepted when num_speakers is absent.
            kwargs["diarization_threshold"] = self.settings.diarization_threshold
        if self.settings.keyterms:
            kwargs["keyterms"] = self.settings.keyterms

        raw = self._call(kwargs, source_url=source_url, file_path=file_path)
        return Transcript.from_raw(raw, provider=self.name, model_id=self.model_id)

    def _call(
        self,
        kwargs: dict[str, Any],
        *,
        source_url: str | None,
        file_path: str | None,
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            handle = None
            try:
                if file_path:
                    handle = open(file_path, "rb")
                    call_kwargs = dict(kwargs, file=handle)
                else:
                    call_kwargs = dict(kwargs, source_url=source_url)
                result = self._client.speech_to_text.convert(**call_kwargs)
                return _to_dict(result)
            except ApiError as err:
                last_error = err
                if err.status_code not in RETRY_STATUSES:
                    raise TranscriptionError(
                        f"ElevenLabs rejected the request ({err.status_code}): {err.body}"
                    ) from err
            except (httpx.TimeoutException, httpx.TransportError) as err:
                last_error = err
            finally:
                if handle is not None:
                    handle.close()

            if attempt < MAX_ATTEMPTS:
                delay = 2**attempt * 5
                log.warning(
                    "ElevenLabs attempt %s/%s failed (%s); retrying in %ss",
                    attempt,
                    MAX_ATTEMPTS,
                    last_error,
                    delay,
                )
                time.sleep(delay)

        raise TranscriptionError(f"ElevenLabs failed after {MAX_ATTEMPTS} attempts: {last_error}")


def _to_dict(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        payload = result
    elif hasattr(result, "model_dump"):
        payload = result.model_dump(mode="json", exclude_none=True)
    elif hasattr(result, "dict"):
        payload = result.dict()
    else:
        raise TranscriptionError(f"Unexpected response type from ElevenLabs: {type(result)!r}")

    if "words" not in payload and "transcripts" in payload:
        raise TranscriptionError(
            "Received a multichannel response; this pipeline expects a single transcript."
        )
    return payload
