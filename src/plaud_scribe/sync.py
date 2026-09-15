"""Orchestration: Plaud -> Scribe v2 -> Markdown/SRT/VTT -> Google Drive."""

from __future__ import annotations

import json
import logging
import re
import tempfile
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from . import render, speakers, summary as summary_module
from .config import FORMAT_SUFFIXES, OUT_DIR, RAW_DIR, Config
from .drive import DriveUploader
from .langid import LanguageTagger
from .plaud import PlaudAuthError, PlaudClient, duration_seconds, parse_timestamp
from .segment import build_cues, build_turns
from .state import Store, languages_field
from .stt.base import Transcript, Turn
from .stt.elevenlabs import ElevenLabsProvider

log = logging.getLogger(__name__)

CACHE_VERSION = 1
DAILY_WINDOW = timedelta(hours=24)


@dataclass
class Rendered:
    meta: render.RecordingMeta
    turns: list[Turn]
    documents: dict[str, str]
    languages: list[str] = field(default_factory=list)
    speaker_labels: list[str] = field(default_factory=list)
    summary_cost_usd: float = 0.0
    warnings: list[str] = field(default_factory=list)


@dataclass
class Budget:
    """What is left under the tightest configured limit."""

    remaining_seconds: float
    period: str          # "month" or "24h"
    limit_minutes: float

    @property
    def remaining_minutes(self) -> float:
        return self.remaining_seconds / 60


@dataclass
class SyncResult:
    recording_id: str
    title: str
    status: str
    cost_usd: float = 0.0
    languages: list[str] = field(default_factory=list)
    speakers: list[str] = field(default_factory=list)
    drive_files: dict[str, str] = field(default_factory=dict)
    local_files: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def counts_as_failure(self) -> bool:
        return self.status == "failed"


def slugify(value: str, *, limit: int = 60) -> str:
    normalised = unicodedata.normalize("NFKD", value)
    ascii_only = normalised.encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", ascii_only).strip("-").lower()
    return (cleaned[:limit].rstrip("-")) or "recording"


def basename_for(meta: render.RecordingMeta) -> str:
    when = meta.recorded_at.strftime("%Y-%m-%d_%H%M") if meta.recorded_at else "undated"
    return f"{when}__{slugify(meta.title)}"


def month_folder(meta: render.RecordingMeta) -> str:
    return meta.recorded_at.strftime("%Y-%m") if meta.recorded_at else "undated"


def filename_for(meta: render.RecordingMeta, fmt: str) -> str:
    return f"{basename_for(meta)}.{FORMAT_SUFFIXES[fmt]}"


def month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def cache_path(recording_id: str) -> Path:
    return RAW_DIR / f"{recording_id}.json"


class Pipeline:
    def __init__(
        self,
        cfg: Config,
        *,
        plaud: PlaudClient,
        store: Store,
        provider: ElevenLabsProvider | None = None,
        uploader: DriveUploader | None = None,
    ) -> None:
        self.cfg = cfg
        self.plaud = plaud
        self.store = store
        self._provider = provider
        self._uploader = uploader
        self.tagger = LanguageTagger(cfg.language.expected, min_chars=cfg.language.min_chars)

    @property
    def provider(self) -> ElevenLabsProvider:
        if self._provider is None:
            self._provider = ElevenLabsProvider(self.cfg)
        return self._provider

    @property
    def uploader(self) -> DriveUploader:
        if self._uploader is None:
            self._uploader = DriveUploader(self.cfg)
        return self._uploader

    # --- transcription ---------------------------------------------------

    def transcribe(self, item: dict[str, Any]) -> Transcript:
        """Prefer handing Plaud's presigned URL straight to ElevenLabs.

        That keeps the audio off this host entirely. If the fetch fails on their side we
        download once and upload the bytes instead.
        """
        recording_id = item["id"]
        detail = item if item.get("presigned_url") else self.plaud.get_file(recording_id)
        url = detail.get("presigned_url")
        if not url:
            raise RuntimeError(
                "Plaud returned no audio URL for this recording "
                "(it may still be syncing from the device)"
            )
        try:
            return self.provider.transcribe(source_url=url)
        except Exception as err:
            log.warning("source_url transcription failed (%s); falling back to upload", err)
            with tempfile.TemporaryDirectory() as tmp:
                audio = Path(tmp) / f"{recording_id}.audio"
                _download(url, audio)
                return self.provider.transcribe(file_path=str(audio))

    def write_cache(
        self, recording_id: str, item: dict[str, Any], transcript: Transcript
    ) -> Path:
        path = cache_path(recording_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": CACHE_VERSION,
            "provider": transcript.provider,
            "model_id": transcript.model_id,
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "recording": {
                "id": recording_id,
                "name": item.get("name") or recording_id,
                "start_at": item.get("start_at") or item.get("created_at"),
                "duration_seconds": duration_seconds(item),
            },
            "transcript": transcript.raw,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        return path

    # --- rendering -------------------------------------------------------

    def build_documents(
        self,
        *,
        recording_id: str,
        title: str,
        recorded_at: datetime | None,
        duration: float,
        transcript: Transcript,
        transcribed_at: datetime | None = None,
        resummarize: bool = False,
    ) -> Rendered:
        turns = build_turns(
            transcript.words,
            gap_seconds=self.cfg.segment.gap_seconds,
            max_seconds=self.cfg.segment.max_turn_seconds,
        )
        majority, languages = (None, [])
        if self.cfg.language.tag_segments:
            majority, languages = self.tagger.tag(turns)

        names = speakers.resolve_aliases(self.cfg, recording_id)
        if self.cfg.speakers.llm_naming:
            inferred = speakers.infer_names(turns, self.cfg)
            # Config always wins over inference.
            names = {**inferred, **names}
        speakers.apply_names(turns, names)

        cues = build_cues(transcript.words)
        speakers.apply_names(cues, names)

        meta = render.RecordingMeta(
            recording_id=recording_id,
            title=title,
            recorded_at=recorded_at,
            duration_seconds=duration or _transcript_duration(transcript),
            provider=transcript.provider,
            model_id=transcript.model_id,
            transcribed_at=transcribed_at or datetime.now(timezone.utc),
        )
        labels = speakers.summarise(turns)
        documents = {
            "md": render.render_markdown(
                turns,
                meta,
                majority_language=majority,
                languages=languages,
                speakers=labels,
            ),
            "txt": render.render_text(
                turns, meta, majority_language=majority, speakers=labels
            ),
            "srt": render.render_srt(cues),
            "vtt": render.render_vtt(cues),
            "json": json.dumps(transcript.raw, ensure_ascii=False, indent=2),
        }
        rendered = Rendered(
            meta=meta,
            turns=turns,
            documents=documents,
            languages=languages,
            speaker_labels=labels,
        )
        if self.cfg.summary_enabled:
            self._attach_summary(rendered, force=resummarize)
        return rendered

    def _attach_summary(self, rendered: Rendered, *, force: bool) -> None:
        """Best effort: a missing summary must never cost us the transcript."""
        meta = rendered.meta
        try:
            summary = summary_module.obtain(
                meta.recording_id, rendered.turns, meta, self.cfg, force=force
            )
        except Exception as err:  # noqa: BLE001 - any failure here is non-fatal
            message = f"summary skipped: {err}"
            log.warning("%s (%s)", message, meta.recording_id)
            rendered.warnings.append(message)
            return
        rendered.documents["summary"] = summary_module.render_document(summary, meta)
        rendered.summary_cost_usd = summary.cost_usd

    def render_from_cache(self, recording_id: str, *, resummarize: bool = False) -> Rendered:
        path = cache_path(recording_id)
        if not path.exists():
            raise FileNotFoundError(
                f"No cached transcript for {recording_id}. Run `plaud-scribe transcribe {recording_id}` first."
            )
        payload = json.loads(path.read_text())
        recording = payload.get("recording", {})
        transcript = Transcript.from_raw(
            payload["transcript"],
            provider=payload.get("provider", "elevenlabs"),
            model_id=payload.get("model_id", self.cfg.elevenlabs.model_id),
        )
        return self.build_documents(
            recording_id=recording_id,
            title=recording.get("name") or recording_id,
            recorded_at=parse_timestamp(recording.get("start_at")),
            duration=float(recording.get("duration_seconds") or 0.0),
            transcript=transcript,
            transcribed_at=parse_timestamp(payload.get("fetched_at")),
            resummarize=resummarize,
        )

    # --- output ----------------------------------------------------------

    def write_local(self, rendered: Rendered) -> dict[str, str]:
        base = basename_for(rendered.meta)
        directory = OUT_DIR / month_folder(rendered.meta)
        directory.mkdir(parents=True, exist_ok=True)
        written = {}
        for fmt in self.cfg.drive.formats:
            content = rendered.documents.get(fmt)
            if content is None:
                continue
            path = directory / filename_for(rendered.meta, fmt)
            path.write_text(content, encoding="utf-8")
            written[fmt] = str(path)
        return written

    def upload(self, rendered: Rendered, existing: dict[str, str]) -> dict[str, str]:
        folder = self.uploader.folder_for(month_folder(rendered.meta))
        uploaded = {}
        for fmt in self.cfg.drive.formats:
            content = rendered.documents.get(fmt)
            if content is None:
                # "summary" is absent when generation failed; the warning is already logged.
                continue
            uploaded[fmt] = self.uploader.upload_text(
                name=filename_for(rendered.meta, fmt),
                content=content,
                folder_id=folder,
                extension=fmt,
                existing_id=existing.get(fmt),
            )
        return uploaded

    # --- budget ----------------------------------------------------------

    def budgets(self) -> list[Budget]:
        """Every configured limit and what is left under it, tightest last."""
        now = datetime.now(timezone.utc)
        windows = [
            ("month", self.cfg.limits.monthly_minutes, month_start(now)),
            ("24h", self.cfg.limits.daily_minutes, now - DAILY_WINDOW),
        ]
        found = []
        for period, minutes, since in windows:
            if minutes <= 0:
                continue
            used = self.store.transcribed_seconds_since(since.isoformat(timespec="seconds"))
            found.append(Budget(max(0.0, minutes * 60 - used), period, minutes))
        return sorted(found, key=lambda b: b.remaining_seconds, reverse=True)

    def budget(self) -> Budget | None:
        """The binding limit, or None when nothing is capped."""
        found = self.budgets()
        return found[-1] if found else None

    # --- top level -------------------------------------------------------

    def process(
        self,
        item: dict[str, Any],
        *,
        upload: bool = True,
        force: bool = False,
        ignore_limits: bool = False,
    ) -> SyncResult:
        recording_id = item["id"]
        title = item.get("name") or recording_id
        recorded_at = parse_timestamp(item.get("start_at") or item.get("created_at"))
        duration = duration_seconds(item)
        record = self.store.note_seen(
            recording_id,
            title,
            recorded_at.isoformat() if recorded_at else None,
            duration,
        )

        needs_transcription = force or not cache_path(recording_id).exists()
        if needs_transcription and not ignore_limits:
            budget = self.budget()
            # A zero duration means Plaud did not report one; let it through rather
            # than stall on a recording whose cost we cannot predict.
            if budget is not None and duration > budget.remaining_seconds:
                return SyncResult(
                    recording_id=recording_id,
                    title=title,
                    status="skipped",
                    error=(
                        f"limit reached: needs {duration / 60:.0f} min, "
                        f"{budget.remaining_minutes:.0f} min left of "
                        f"{budget.limit_minutes:.0f} min per {budget.period}"
                    ),
                )

        try:
            if needs_transcription:
                transcript = self.transcribe(item)
                self.write_cache(recording_id, item, transcript)
            rendered = self.render_from_cache(recording_id)
            local = self.write_local(rendered)
            drive_files = self.upload(rendered, record.drive) if upload else {}
            cost = self.provider.estimate_cost(rendered.meta.duration_seconds) + rendered.summary_cost_usd

            self.store.mark_done(
                recording_id,
                duration_seconds=rendered.meta.duration_seconds or None,
                provider=rendered.meta.provider,
                model_id=rendered.meta.model_id,
                cost_usd=cost,
                languages=languages_field(rendered.languages),
                speaker_count=len(rendered.speaker_labels),
                transcribed_at=rendered.meta.transcribed_at.isoformat()
                if rendered.meta.transcribed_at
                else None,
                uploaded_at=datetime.now(timezone.utc).isoformat(timespec="seconds")
                if drive_files
                else None,
                drive_files={**record.drive, **drive_files} if drive_files else None,
            )
            return SyncResult(
                recording_id=recording_id,
                title=title,
                status="done",
                cost_usd=cost,
                languages=rendered.languages,
                speakers=rendered.speaker_labels,
                drive_files=drive_files,
                local_files=local,
                warnings=rendered.warnings,
            )
        except PlaudAuthError:
            # Not this recording's fault — let it stop the run instead of poisoning state.
            raise
        except Exception as err:
            log.exception("Failed to process %s", recording_id)
            self.store.mark_failed(recording_id, f"{type(err).__name__}: {err}")
            return SyncResult(
                recording_id=recording_id,
                title=title,
                status="failed",
                error=f"{type(err).__name__}: {err}",
            )

    def select(
        self,
        *,
        since: datetime | None = None,
        limit: int | None = None,
        retry_failed: bool = False,
    ) -> list[dict[str, Any]]:
        """Recordings that still need work, newest first."""
        selected = []
        for item in self.plaud.iter_files(since=since):
            recording_id = item.get("id")
            if not recording_id:
                continue
            record = self.store.get(recording_id)
            done = record is not None and record.status == "done"
            failed = record is not None and record.status == "failed"
            if done or (failed and not retry_failed):
                continue
            selected.append(item)
            if limit and len(selected) >= limit:
                break
        return selected


def _download(url: str, destination: Path) -> None:
    with httpx.stream("GET", url, timeout=300.0, follow_redirects=True) as response:
        response.raise_for_status()
        with destination.open("wb") as handle:
            for chunk in response.iter_bytes(chunk_size=1 << 20):
                handle.write(chunk)


def _transcript_duration(transcript: Transcript) -> float:
    ends = [w.end for w in transcript.words if w.end is not None]
    return max(ends) if ends else 0.0
