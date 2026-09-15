import re

from plaud_scribe import render, speakers
from plaud_scribe.langid import LanguageTagger
from plaud_scribe.segment import build_cues, build_turns
from plaud_scribe.stt.base import default_speaker_label


def _meta(envelope):
    from plaud_scribe.plaud import parse_timestamp

    recording = envelope["recording"]
    return render.RecordingMeta(
        recording_id=recording["id"],
        title=recording["name"],
        recorded_at=parse_timestamp(recording["start_at"]),
        duration_seconds=recording["duration_seconds"],
        provider="elevenlabs",
        model_id="scribe_v2",
    )


def test_markdown_front_matter_and_turns(transcript, envelope):
    turns = build_turns(transcript.words)
    majority, languages = LanguageTagger(["en", "de", "pl", "pt", "ru"]).tag(turns)
    speakers.apply_names(turns, {"speaker_0": "Grzegorz"})
    text = render.render_markdown(
        turns,
        _meta(envelope),
        majority_language=majority,
        languages=languages,
        speakers=speakers.summarise(turns),
    )

    assert text.startswith("---\n")
    assert text.count("---") >= 2
    assert 'title: "Weekly sync / Wochentreffen"' in text
    assert "plaud_id: rec_test_0001" in text
    assert "model: elevenlabs/scribe_v2" in text
    assert "**[00:00:04] Grzegorz:**" in text
    assert "**[00:00:11] Speaker 2:**" in text


def test_only_minority_languages_are_tagged(transcript, envelope):
    turns = build_turns(transcript.words)
    majority, languages = LanguageTagger(["en", "de", "pl", "pt", "ru"]).tag(turns)
    text = render.render_markdown(turns, _meta(envelope), majority_language=majority, languages=languages)

    for turn in turns:
        line = next(l for l in text.splitlines() if turn.text[:20] in l)
        if turn.language and turn.language != majority:
            assert f"_({turn.language})_" in line
        else:
            assert "_(" not in line


def test_srt_is_well_formed(transcript):
    cues = build_cues(transcript.words)
    srt = render.render_srt(cues)
    blocks = [b for b in srt.split("\n\n") if b.strip()]
    assert len(blocks) == len(cues)

    first = blocks[0].splitlines()
    assert first[0] == "1"
    assert re.fullmatch(r"\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3}", first[1])
    assert first[2].startswith("Speaker 1: ")

    stamps = re.findall(r"(\d{2}):(\d{2}):(\d{2}),(\d{3}) --> (\d{2}):(\d{2}):(\d{2}),(\d{3})", srt)
    for h1, m1, s1, ms1, h2, m2, s2, ms2 in stamps:
        start = int(h1) * 3600 + int(m1) * 60 + int(s1) + int(ms1) / 1000
        end = int(h2) * 3600 + int(m2) * 60 + int(s2) + int(ms2) / 1000
        assert end > start


def test_vtt_header_and_dot_separator(transcript):
    vtt = render.render_vtt(build_cues(transcript.words))
    assert vtt.startswith("WEBVTT\n")
    assert re.search(r"\d{2}:\d{2}:\d{2}\.\d{3} --> ", vtt)
    assert "," not in vtt.splitlines()[2]


def test_speaker_label_is_one_based():
    assert default_speaker_label("speaker_0") == "Speaker 1"
    assert default_speaker_label("agent") == "agent"


def test_duration_formatting():
    assert render.format_duration(62.5) == "1m02s"
    assert render.format_duration(3720) == "1h02m"
    assert render.format_clock(3661) == "01:01:01"


def test_plain_text_has_no_markup(transcript, envelope):
    turns = build_turns(transcript.words)
    majority, _ = LanguageTagger(["en", "de", "pl", "pt", "ru"]).tag(turns)
    text = render.render_text(
        turns, _meta(envelope), majority_language=majority, speakers=speakers.summarise(turns)
    )
    lines = text.splitlines()
    assert lines[0] == "Weekly sync / Wochentreffen"
    assert "Duration 1m02s" in lines[1]
    assert "Speakers: Speaker 1, Speaker 2" in lines[1]
    assert lines[2] == ""
    assert re.match(r"^\[00:00:04\] Speaker 1( \([a-z]{2}\))?: ", lines[3])
    assert "**" not in text and "---" not in text and "#" not in text
    # Every spoken word survives the rendering.
    spoken = " ".join(t.text for t in turns)
    assert all(word in text for word in spoken.split()[:50])


def test_llm_transcript_is_one_line_per_turn(transcript):
    turns = build_turns(transcript.words)
    body = render.transcript_for_llm(turns)
    assert len(body.splitlines()) == len(turns)
    assert re.match(r"^\[\d\d:\d\d:\d\d\] Speaker \d+: ", body.splitlines()[0])
