"""Configuration loading: TOML file plus environment overrides."""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("PLAUD_SCRIBE_CONFIG_DIR", Path.home() / ".config/plaud-scribe"))
DATA_DIR = Path(os.environ.get("PLAUD_SCRIBE_DATA_DIR", Path.home() / ".local/share/plaud-scribe"))
STATE_DIR = Path(os.environ.get("PLAUD_SCRIBE_STATE_DIR", Path.home() / ".local/state/plaud-scribe"))

CONFIG_FILE = CONFIG_DIR / "config.toml"
GOOGLE_CLIENT_FILE = CONFIG_DIR / "google_client.json"
GOOGLE_TOKEN_FILE = CONFIG_DIR / "google_token.json"
RAW_DIR = DATA_DIR / "raw"
OUT_DIR = DATA_DIR / "out"
STATE_DB = STATE_DIR / "state.db"

DEFAULT_LANGUAGES = ["en", "de", "pl", "pt", "ru"]


class ConfigError(RuntimeError):
    pass


@dataclass
class ElevenLabsConfig:
    api_key: str = ""
    model_id: str = "scribe_v2"
    num_speakers: int | None = None
    diarization_threshold: float | None = None
    keyterms: list[str] = field(default_factory=list)
    tag_audio_events: bool = True
    timeout_seconds: float = 3600.0
    # $/hour. Keyterm prompting adds $0.05/h when keyterms are supplied.
    price_per_hour: float = 0.22
    keyterm_price_per_hour: float = 0.05

    def hourly_rate(self) -> float:
        return self.price_per_hour + (self.keyterm_price_per_hour if self.keyterms else 0.0)


@dataclass
class PlaudConfig:
    cli: str = "plaud"
    api_base: str = "https://platform.plaud.ai/developer/api"
    refresh_url: str = "https://platform.plaud.ai/developer/api/oauth/third-party/access-token/refresh"
    client_id: str = "client_f9e0b214-c11f-434b-8b95-c4497d1feb81"
    token_file: str = "~/.plaud/tokens.json"
    page_size: int = 50


# Output format -> file suffix. "summary" is a separate Markdown document.
FORMAT_SUFFIXES = {
    "md": "md",
    "txt": "txt",
    "summary": "summary.md",
    "srt": "srt",
    "vtt": "vtt",
    "json": "json",
}
DEFAULT_FORMATS = ["txt", "summary"]


@dataclass
class DriveConfig:
    root_folder: str = "Plaud Transcripts"
    formats: list[str] = field(default_factory=lambda: list(DEFAULT_FORMATS))
    oauth_port: int = 8765


# $/1M tokens, input and output, for the spend figure shown by `plaud-scribe status`.
MODEL_PRICES_PER_MTOK = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


@dataclass
class SummaryConfig:
    """Claude-written summary, produced only when "summary" is in [drive] formats."""

    # Or leave empty and export ANTHROPIC_API_KEY instead.
    api_key: str = ""
    model: str = "claude-sonnet-5"
    # "auto" = the language that dominates the recording; or a code such as "en".
    language: str = "auto"
    effort: str = "medium"
    max_output_tokens: int = 8000
    # Server-side refusal fallback: re-run on another model if this one declines.
    fallbacks: bool = True
    # $/1M tokens. 0 means "look the model up in MODEL_PRICES_PER_MTOK", so the cost
    # figure follows the model instead of silently going stale when you change it.
    input_price_per_mtok: float = 0.0
    output_price_per_mtok: float = 0.0

    def prices(self) -> tuple[float, float]:
        if self.input_price_per_mtok or self.output_price_per_mtok:
            return self.input_price_per_mtok, self.output_price_per_mtok
        known = MODEL_PRICES_PER_MTOK.get(self.model)
        if known is not None:
            return known
        # Unknown model: over-estimate rather than under-report what was spent.
        dearest = max(MODEL_PRICES_PER_MTOK.values())
        logging.getLogger(__name__).warning(
            "No price known for %s; reporting cost at %s/%s per Mtok. Set [summary] "
            "input_price_per_mtok and output_price_per_mtok to correct it.",
            self.model, *dearest,
        )
        return dearest


@dataclass
class LimitsConfig:
    """Ceilings on how much audio may be sent for transcription.

    The monthly window is the calendar month, matching how the ElevenLabs key's own
    usage cap refreshes. The daily window is a rolling 24 hours and is off by default;
    it exists as a circuit breaker against a runaway loop burning the month in one go.
    Either limit set to 0 is disabled; whichever is tighter applies.
    """

    # Audio minutes transcribable per calendar month. 0 disables.
    monthly_minutes: float = 2700.0
    # Audio minutes transcribable per rolling 24h. 0 disables.
    daily_minutes: float = 0.0


@dataclass
class SyncConfig:
    """Which recordings `sync` is allowed to touch.

    start_date is a hard floor, not a rolling window: anything recorded before it is
    never transcribed, however far back a run happens to look. That keeps an old archive
    out of the way permanently, and survives the host being off for a while.
    """

    # "YYYY-MM-DD", in UTC. Empty means no floor.
    start_date: str = ""

    def floor(self) -> "datetime | None":
        if not self.start_date.strip():
            return None
        try:
            parsed = datetime.strptime(self.start_date.strip(), "%Y-%m-%d")
        except ValueError as err:
            raise ConfigError(
                f'[sync] start_date must be YYYY-MM-DD, got {self.start_date!r}'
            ) from err
        return parsed.replace(tzinfo=timezone.utc)


@dataclass
class SegmentConfig:
    gap_seconds: float = 2.0
    max_turn_seconds: float = 60.0


@dataclass
class LanguageConfig:
    expected: list[str] = field(default_factory=lambda: list(DEFAULT_LANGUAGES))
    tag_segments: bool = True
    min_chars: int = 12


@dataclass
class SpeakerConfig:
    aliases: dict[str, str] = field(default_factory=dict)
    per_recording: dict[str, dict[str, str]] = field(default_factory=dict)
    llm_naming: bool = False
    llm_model: str = "claude-sonnet-5"


@dataclass
class Config:
    elevenlabs: ElevenLabsConfig = field(default_factory=ElevenLabsConfig)
    plaud: PlaudConfig = field(default_factory=PlaudConfig)
    drive: DriveConfig = field(default_factory=DriveConfig)
    sync: SyncConfig = field(default_factory=SyncConfig)
    segment: SegmentConfig = field(default_factory=SegmentConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    language: LanguageConfig = field(default_factory=LanguageConfig)
    speakers: SpeakerConfig = field(default_factory=SpeakerConfig)
    summary: SummaryConfig = field(default_factory=SummaryConfig)
    path: Path = CONFIG_FILE

    @property
    def summary_enabled(self) -> bool:
        return "summary" in self.drive.formats

    def require_anthropic_key(self) -> str:
        if not self.summary.api_key:
            raise ConfigError(
                "No Anthropic API key. Set ANTHROPIC_API_KEY or add "
                f"[summary] api_key to {self.path}"
            )
        return self.summary.api_key

    def require_elevenlabs_key(self) -> str:
        if not self.elevenlabs.api_key:
            raise ConfigError(
                "No ElevenLabs API key. Set ELEVENLABS_API_KEY or add "
                f"[elevenlabs] api_key to {self.path}"
            )
        return self.elevenlabs.api_key


def _section(raw: dict, name: str) -> dict:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a table")
    return value


def _apply(obj, data: dict, name: str) -> None:
    """Copy known keys from a TOML table onto a dataclass instance."""
    known = set(vars(obj))
    for key, value in data.items():
        if key not in known:
            raise ConfigError(f"Unknown key [{name}] {key}")
        setattr(obj, key, value)


def load(path: Path | None = None) -> Config:
    path = path or CONFIG_FILE
    cfg = Config(path=path)
    raw: dict = {}
    if path.exists():
        with path.open("rb") as handle:
            raw = tomllib.load(handle)

    _apply(cfg.elevenlabs, _section(raw, "elevenlabs"), "elevenlabs")
    _apply(cfg.plaud, _section(raw, "plaud"), "plaud")
    _apply(cfg.drive, _section(raw, "drive"), "drive")
    _apply(cfg.sync, _section(raw, "sync"), "sync")
    _apply(cfg.segment, _section(raw, "segment"), "segment")
    _apply(cfg.limits, _section(raw, "limits"), "limits")
    _apply(cfg.language, _section(raw, "language"), "language")
    _apply(cfg.summary, _section(raw, "summary"), "summary")

    speakers = _section(raw, "speakers")
    cfg.speakers.aliases = dict(speakers.get("aliases", {}))
    cfg.speakers.per_recording = {
        rid: dict(mapping) for rid, mapping in speakers.get("per_recording", {}).items()
    }
    cfg.speakers.llm_naming = bool(speakers.get("llm_naming", False))
    cfg.speakers.llm_model = speakers.get("llm_model", cfg.speakers.llm_model)

    env_key = os.environ.get("ELEVENLABS_API_KEY")
    if env_key:
        cfg.elevenlabs.api_key = env_key
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if anthropic_key:
        cfg.summary.api_key = anthropic_key
    cfg.sync.floor()  # validate the date format at load time, not mid-run
    for name in ("monthly_minutes", "daily_minutes"):
        if getattr(cfg.limits, name) < 0:
            raise ConfigError(f"[limits] {name} must be 0 (no limit) or positive")
    unknown = [f for f in cfg.drive.formats if f not in FORMAT_SUFFIXES]
    if unknown:
        raise ConfigError(
            f"Unknown [drive] formats {unknown}; choose from {sorted(FORMAT_SUFFIXES)}"
        )
    # 0 is the "let the model decide" sentinel in TOML, where null does not exist.
    if not cfg.elevenlabs.num_speakers:
        cfg.elevenlabs.num_speakers = None
    if not cfg.elevenlabs.diarization_threshold:
        cfg.elevenlabs.diarization_threshold = None
    return cfg


def ensure_dirs() -> None:
    for directory in (CONFIG_DIR, DATA_DIR, STATE_DIR, RAW_DIR, OUT_DIR):
        directory.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.chmod(0o700)
