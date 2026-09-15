import json
from pathlib import Path

import pytest

from plaud_scribe.stt.base import Transcript

FIXTURE = Path(__file__).parent / "fixtures" / "scribe_sample.json"


@pytest.fixture
def envelope() -> dict:
    return json.loads(FIXTURE.read_text())


@pytest.fixture
def transcript(envelope) -> Transcript:
    return Transcript.from_raw(envelope["transcript"], provider="elevenlabs", model_id="scribe_v2")
