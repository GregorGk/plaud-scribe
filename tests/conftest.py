import importlib
import json
from pathlib import Path

import pytest

from plaud_scribe.stt.base import Transcript

FIXTURE = Path(__file__).parent / "fixtures" / "scribe_sample.json"

# Every module that binds a data path at import time. Patching only plaud_scribe.config
# is not enough: `from .config import OUT_DIR` copies the value, so each importer keeps
# pointing at the real directory. That is how a test once wrote empty files into the
# live output folder. Add a module here if it starts importing a path by name.
_PATHS = {
    "plaud_scribe.config": (
        "CONFIG_DIR", "DATA_DIR", "STATE_DIR", "CONFIG_FILE", "GOOGLE_CLIENT_FILE",
        "GOOGLE_TOKEN_FILE", "RAW_DIR", "OUT_DIR", "STATE_DB",
    ),
    "plaud_scribe.sync": ("RAW_DIR", "OUT_DIR"),
    "plaud_scribe.summary": ("RAW_DIR",),
    "plaud_scribe.cli": ("STATE_DB",),
    "plaud_scribe.drive": ("DATA_DIR", "GOOGLE_CLIENT_FILE", "GOOGLE_TOKEN_FILE", "FOLDER_CACHE"),
}


def _isolated(root: Path) -> dict[str, Path]:
    cfg, data, state = root / "config", root / "data", root / "state"
    return {
        "CONFIG_DIR": cfg,
        "DATA_DIR": data,
        "STATE_DIR": state,
        "CONFIG_FILE": cfg / "config.toml",
        "GOOGLE_CLIENT_FILE": cfg / "google_client.json",
        "GOOGLE_TOKEN_FILE": cfg / "google_token.json",
        "RAW_DIR": data / "raw",
        "OUT_DIR": data / "out",
        "STATE_DB": state / "state.db",
        "FOLDER_CACHE": data / "drive_folders.json",
    }


@pytest.fixture(autouse=True)
def isolate_data_dirs(tmp_path_factory, monkeypatch):
    """No test may read or write the real ~/.config, ~/.local/share or ~/.local/state."""
    targets = _isolated(tmp_path_factory.mktemp("home"))
    for module_name, names in _PATHS.items():
        module = importlib.import_module(module_name)
        for name in names:
            monkeypatch.setattr(module, name, targets[name])
    yield targets


@pytest.fixture
def envelope() -> dict:
    return json.loads(FIXTURE.read_text())


@pytest.fixture
def transcript(envelope) -> Transcript:
    return Transcript.from_raw(envelope["transcript"], provider="elevenlabs", model_id="scribe_v2")
