from plaud_scribe.segment import build_cues, build_turns


def test_turns_split_on_speaker_change(transcript):
    turns = build_turns(transcript.words)
    speakers = [turn.speaker_id for turn in turns]
    assert speakers[:4] == ["speaker_0", "speaker_1", "speaker_0", "speaker_2"]


def test_long_silence_splits_the_same_speaker(transcript):
    turns = build_turns(transcript.words, gap_seconds=2.0)
    speaker_two = [turn for turn in turns if turn.speaker_id == "speaker_2"]
    assert len(speaker_two) == 2, "a >2s gap must start a new turn"
    assert speaker_two[1].text.startswith("Desculpem")


def test_turn_text_has_no_double_spaces(transcript):
    for turn in build_turns(transcript.words):
        assert "  " not in turn.text
        assert turn.text == turn.text.strip()


def test_audio_events_are_kept(transcript):
    joined = " ".join(turn.text for turn in build_turns(transcript.words))
    assert "(laughter)" in joined


def test_turns_are_monotonic(transcript):
    turns = build_turns(transcript.words)
    assert all(turn.end >= turn.start for turn in turns)
    assert all(b.start >= a.start for a, b in zip(turns, turns[1:]))


def test_cues_respect_the_character_budget(transcript):
    for cue in build_cues(transcript.words, max_chars=40, max_seconds=6.0):
        # The budget is checked before appending a word, so one word may overshoot.
        assert len(cue.text) <= 40 + 30


def test_no_words_are_lost_between_turns_and_cues(transcript):
    turn_text = " ".join(t.text for t in build_turns(transcript.words))
    cue_text = " ".join(c.text for c in build_cues(transcript.words))
    assert turn_text.split() == cue_text.split()


def test_missing_spacing_entries_still_separate_words():
    from plaud_scribe.stt.base import Word

    words = [
        Word(text="сегодня.", start=0.0, end=0.5, speaker_id="speaker_0"),
        Word(text="(laughter)", start=0.6, end=1.2, speaker_id="speaker_0", type="audio_event"),
    ]
    assert build_turns(words)[0].text == "сегодня. (laughter)"
