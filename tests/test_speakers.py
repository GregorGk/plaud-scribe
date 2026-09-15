from plaud_scribe.config import Config
from plaud_scribe.speakers import _normalise_key, _parse_mapping, resolve_aliases


def test_alias_key_forms_are_equivalent():
    assert _normalise_key("speaker_0") == "speaker_0"
    assert _normalise_key("Speaker 1") == "speaker_0"
    assert _normalise_key("0") == "speaker_0"


def test_per_recording_aliases_override_global():
    cfg = Config()
    cfg.speakers.aliases = {"speaker_0": "Grzegorz"}
    cfg.speakers.per_recording = {"rec_1": {"Speaker 1": "Grzegorz G.", "speaker_1": "Anna"}}
    assert resolve_aliases(cfg, "rec_1") == {"speaker_0": "Grzegorz G.", "speaker_1": "Anna"}
    assert resolve_aliases(cfg, "rec_other") == {"speaker_0": "Grzegorz"}


def test_inferred_names_are_filtered_to_known_speakers():
    known = {"speaker_0", "speaker_1"}
    text = 'Here you go: {"speaker_0": "Anna", "speaker_9": "Ghost", "speaker_1": ""}'
    assert _parse_mapping(text, known) == {"speaker_0": "Anna"}


def test_unparseable_naming_response_is_ignored():
    assert _parse_mapping("I could not identify anyone.", {"speaker_0"}) == {}
