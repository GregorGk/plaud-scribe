from plaud_scribe.plaud import _extract_items, duration_seconds, parse_timestamp
from plaud_scribe.sync import basename_for, month_folder, slugify
from plaud_scribe.render import RecordingMeta


def test_slugify_strips_diacritics_and_punctuation():
    assert slugify("Weekly sync / Wochentreffen") == "weekly-sync-wochentreffen"
    assert slugify("Spotkanie z Łukaszem") == "spotkanie-z-ukaszem"
    assert slugify("///") == "recording"


def test_filename_is_sortable(envelope):
    meta = RecordingMeta(
        recording_id="rec_test_0001",
        title=envelope["recording"]["name"],
        recorded_at=parse_timestamp(envelope["recording"]["start_at"]),
        duration_seconds=62.5,
        provider="elevenlabs",
        model_id="scribe_v2",
    )
    assert basename_for(meta) == "2026-08-24_1403__weekly-sync-wochentreffen"
    assert month_folder(meta) == "2026-08"


def test_duration_is_milliseconds():
    assert duration_seconds({"duration": 62500}) == 62.5
    assert duration_seconds({}) == 0.0


def test_timestamp_parsing_accepts_iso_and_epoch():
    assert parse_timestamp("2026-08-24T14:03:00Z").hour == 14
    assert parse_timestamp(1756044180).year == 2025
    assert parse_timestamp(1756044180000).year == 2025
    assert parse_timestamp(None) is None
    assert parse_timestamp("nonsense") is None


def test_list_envelopes_are_unwrapped():
    assert _extract_items([{"id": "a"}]) == [{"id": "a"}]
    assert _extract_items({"data": [{"id": "b"}]}) == [{"id": "b"}]
    assert _extract_items({"data": {"items": [{"id": "c"}]}}) == [{"id": "c"}]
    assert _extract_items({"nothing": 1}) == []


def test_example_config_loads(tmp_path):
    import shutil
    from plaud_scribe import config as config_module

    target = tmp_path / "config.toml"
    shutil.copy("config.example.toml", target)
    cfg = config_module.load(target)

    assert cfg.elevenlabs.model_id == "scribe_v2"
    # 0 in TOML is the "let the model decide" sentinel and must become None.
    assert cfg.elevenlabs.num_speakers is None
    assert cfg.elevenlabs.diarization_threshold is None
    assert cfg.language.expected == ["en", "de", "pl", "pt", "ru"]
    assert cfg.drive.formats == ["md", "txt", "summary"]
    assert cfg.summary_enabled is True
    assert cfg.summary.model == "claude-opus-5"
    assert cfg.speakers.llm_naming is False


def test_unknown_output_format_is_rejected(tmp_path):
    from plaud_scribe import config as config_module

    target = tmp_path / "config.toml"
    target.write_text('[drive]\nformats = ["md", "docx"]\n')
    with __import__("pytest").raises(config_module.ConfigError):
        config_module.load(target)


def test_summary_has_its_own_filename(envelope):
    from plaud_scribe.sync import filename_for

    meta = RecordingMeta(
        recording_id="rec_test_0001",
        title=envelope["recording"]["name"],
        recorded_at=parse_timestamp(envelope["recording"]["start_at"]),
        duration_seconds=62.5,
        provider="elevenlabs",
        model_id="scribe_v2",
    )
    assert filename_for(meta, "md") == "2026-08-24_1403__weekly-sync-wochentreffen.md"
    assert filename_for(meta, "txt") == "2026-08-24_1403__weekly-sync-wochentreffen.txt"
    assert filename_for(meta, "summary") == "2026-08-24_1403__weekly-sync-wochentreffen.summary.md"


def test_unknown_config_key_is_rejected(tmp_path):
    from plaud_scribe import config as config_module

    target = tmp_path / "config.toml"
    target.write_text('[elevenlabs]\nmodle_id = "typo"\n')
    with __import__("pytest").raises(config_module.ConfigError):
        config_module.load(target)


def test_keyterms_change_the_hourly_rate():
    from plaud_scribe.config import Config

    cfg = Config()
    assert cfg.elevenlabs.hourly_rate() == 0.22
    cfg.elevenlabs.keyterms = ["Kubernetes"]
    assert round(cfg.elevenlabs.hourly_rate(), 4) == 0.27
