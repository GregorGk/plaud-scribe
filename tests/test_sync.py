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
    assert cfg.drive.formats == ["txt", "summary"]
    assert cfg.summary_enabled is True
    assert cfg.sync.start_date == "2026-09-16"
    assert cfg.summary.model == "claude-sonnet-5"
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


def _cfg_with_floor(date):
    from plaud_scribe.config import Config
    cfg = Config()
    cfg.sync.start_date = date
    return cfg


def test_start_date_parses_to_utc_midnight():
    from datetime import datetime, timezone
    cfg = _cfg_with_floor("2026-09-16")
    assert cfg.sync.floor() == datetime(2026, 9, 16, tzinfo=timezone.utc)
    assert _cfg_with_floor("  ").sync.floor() is None


def test_bad_start_date_is_rejected_at_load(tmp_path):
    import pytest
    from plaud_scribe import config as config_module

    target = tmp_path / "config.toml"
    target.write_text('[sync]\nstart_date = "16/09/2026"\n')
    with pytest.raises(config_module.ConfigError, match="YYYY-MM-DD"):
        config_module.load(target)


def test_select_never_reaches_behind_the_start_date(tmp_path, monkeypatch):
    """Even --all must not pull in recordings from before the floor."""
    from plaud_scribe import sync as sync_module
    from plaud_scribe.state import Store

    monkeypatch.setattr(sync_module, "RAW_DIR", tmp_path)
    feed = [
        {"id": "new2", "name": "after",  "start_at": "2026-09-20T10:00:00Z", "duration": 60_000},
        {"id": "new1", "name": "on the day", "start_at": "2026-09-16T00:00:01Z", "duration": 60_000},
        {"id": "old1", "name": "before", "start_at": "2026-09-15T23:59:59Z", "duration": 60_000},
        {"id": "old2", "name": "long before", "start_at": "2026-06-01T10:00:00Z", "duration": 60_000},
    ]

    class FakePlaud:
        def iter_files(self, *, since=None, **kw):
            for item in feed:
                yield item

    with Store(tmp_path / "s.db") as store:
        pipeline = sync_module.Pipeline(
            _cfg_with_floor("2026-09-16"), plaud=FakePlaud(), store=store
        )
        picked = [i["id"] for i in pipeline.select(since=None)]

    assert picked == ["new2", "new1"], "anything before the floor must be dropped"
