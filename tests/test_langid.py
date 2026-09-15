from plaud_scribe.langid import LanguageTagger
from plaud_scribe.segment import build_turns


def test_detects_each_language_in_a_mixed_file(transcript):
    turns = build_turns(transcript.words)
    tagger = LanguageTagger(["en", "de", "pl", "pt", "ru"])
    majority, languages = tagger.tag(turns)

    assert majority in {"de", "pl", "pt", "en", "ru"}
    assert set(languages) >= {"de", "pl", "pt", "ru"}
    by_text = {turn.text[:12]: turn.language for turn in turns}
    assert by_text["Guten Morgen"] == "de"
    assert by_text["Dobra, mam d"] == "pl"
    assert by_text["Давайте начн"] == "ru"


def test_short_turns_are_left_untagged():
    tagger = LanguageTagger(["en", "de"], min_chars=12)
    assert tagger.detect("ok") is None
