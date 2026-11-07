"""Telling the other side's voices apart, and naming them. The voices here are
given as turns; sherpa-onnx itself is only run by hand on real audio."""
import json
import re

import llm
import parser as tparser
import speakers
from transcriber import Line, Live, to_vtt


def them(start, end, text="..."):
    return Line("them", start, end, text)


def test_their_lines_are_numbered_by_voice_in_the_order_they_first_speak():
    lines = [them(0, 40), Line("me", 40, 60, "ok"), them(60, 90), them(100, 120),
             them(130, 150)]
    turns = [(7, 1, 42), (3, 60, 95), (7, 99, 121)]     # voice 7 opens the call

    who = [line.who for line in speakers.assign(lines, turns)]

    # The last line overlaps no voice: the nearest one said it.
    assert who == ["Speaker 1", "me", "Speaker 2", "Speaker 1", "Speaker 1"]


def test_one_voice_on_their_side_stays_them():
    lines = [them(0, 40), them(50, 80)]
    assert speakers.assign(lines, [(0, 0, 40), (0, 50, 80)]) == lines


def test_a_voice_heard_for_a_few_seconds_is_not_another_person():
    """What a real call looked like: one voice, and scraps of it split off."""
    lines = [them(0, 30), them(31, 33), them(40, 70), them(71, 74)]
    turns = [(0, 0, 30), (5, 31, 33), (0, 40, 70), (9, 71, 74)]

    assert speakers.assign(lines, turns) == lines


def test_a_stray_voice_goes_to_the_nearest_real_one():
    lines = [them(0, 30), them(31, 33), them(40, 70)]
    turns = [(0, 0, 30), (5, 31, 33), (1, 40, 70)]

    who = [line.who for line in speakers.assign(lines, turns)]

    assert who == ["Speaker 1", "Speaker 1", "Speaker 2"]


def test_a_failure_keeps_the_lines_as_they_were(tmp_path, monkeypatch):
    def broken(status):
        raise OSError("no network")
    monkeypatch.setattr(speakers, "ensure_models", broken)
    lines = [them(0, 4)]
    assert speakers.relabel(lines, tmp_path) == lines


def test_the_vtt_carries_the_voices():
    vtt = to_vtt([Line("Speaker 2", 1, 2, "hello"), Line("me", 3, 4, "hi")])
    assert "<v Speaker 2>hello</v>" in vtt and "<v Me>hi</v>" in vtt


def test_a_call_ends_with_its_voices_told_apart(tmp_path):
    for name in ("system.wav", "mic.wav"):
        (tmp_path / name).write_bytes(b"")
    live = Live(tmp_path, transcribe=lambda s, p: "x",
                relabel=lambda lines, folder, status: [Line("Speaker 1", 0, 1, "hello")])
    live.lines = [them(0, 1, "hello")]
    live.start()

    vtt = live.finish()

    assert tparser.parse(vtt).turns == [("Speaker 1", "hello")]


# ---------------------------------------------------------------- names

NOTES = {"title": "Sync with Speaker 1", "participants": ["Me", "Speaker 1", "Speaker 10"],
         "summary": "Speaker 1 agreed; Speaker 10 did not.",
         "action_items": [{"owner": "Speaker 1", "task": "Ask Speaker 10", "deadline": None}],
         "_meta": {"note": "Speaker 1"}}


def test_a_name_replaces_the_label_everywhere_but_only_whole():
    out = speakers.rename(NOTES, "Speaker 1", "Anna")

    assert out["title"] == "Sync with Anna"
    assert out["participants"] == ["Me", "Anna", "Speaker 10"]
    assert out["summary"] == "Anna agreed; Speaker 10 did not."
    assert out["action_items"][0] == {"owner": "Anna", "task": "Ask Speaker 10", "deadline": None}
    assert out["_meta"] == {"note": "Speaker 1"}            # the app's own fields are left
    assert out[speakers.NAMES] == {"Speaker 1": "Anna"}


def test_renaming_again_keeps_what_the_transcript_says():
    out = speakers.rename(speakers.rename(NOTES, "Speaker 1", "Ann"), "Ann", "Anna Berg")
    assert out[speakers.NAMES] == {"Speaker 1": "Anna Berg"}
    assert speakers.named([("Speaker 1", "hi"), ("Me", "yo")], out) == [
        ("Anna Berg", "hi"), ("Me", "yo")]


def test_the_model_is_told_who_the_voices_are(monkeypatch):
    prompts = []
    monkeypatch.setattr(llm, "generate_json",
                        lambda prompt, schema, model: prompts.append(prompt) or {"title": "t"})
    llm.summarize([("Me", "hi"), ("Speaker 1", "hello Me")], {"source": "Call.vtt"}, model="m")
    assert '"Speaker N" and "Them" are the other people' in prompts[0]


# ---------------------------------------------------------------- names from Teams

def said(tmp_path, events):
    (tmp_path / speakers.SPEAKING_FILE).write_text(json.dumps(events))
    return tmp_path


def test_the_speaking_labels_teams_might_show():
    pattern = re.compile(speakers.SPEAKING)
    assert pattern.search("Anna Petrova, speaking")[1] == "Anna Petrova"
    assert pattern.search("Anna is speaking")[1] == "Anna"
    assert pattern.search("Анна говорит")[1] == "Анна"
    assert not pattern.search("Speaking time")
    assert not pattern.search("Anna Petrova, muted")


def test_the_timeline_is_when_each_name_was_shown_speaking(tmp_path):
    folder = said(tmp_path, [[1.0, ["Anna, speaking"]],
                             [4.0, ["Anna, speaking", "Me Myself, speaking", "Mute"]],
                             [5.0, ["Me Myself, speaking"]],
                             [9.0, []],
                             [12.0, ["Anna, speaking"]],
                             [15.5, []]])
    assert speakers.timeline(folder) == [("Anna", 1.0, 5.0), ("Me Myself", 4.0, 9.0),
                                         ("Anna", 12.0, 15.5)]


def test_no_speaking_file_is_no_timeline(tmp_path):
    assert speakers.timeline(tmp_path) == []


def test_each_line_gets_the_name_shown_while_it_was_said():
    """Two people that the voices could not tell apart."""
    lines = [them(0, 10), them(11, 20), Line("me", 21, 25, "ok"), them(26, 30)]
    spans = [("Anna", 0.5, 10), ("Philip", 11, 20), ("Me Myself", 21, 25),
             ("Anna", 26, 29)]

    assert [line.who for line in speakers.put_names(lines, spans)] == [
        "Anna", "Philip", "me", "Anna"]


def test_a_line_teams_missed_takes_its_voice_s_name():
    lines = [Line("Speaker 1", 0, 10, "..."), Line("Speaker 2", 11, 20, "..."),
             Line("Speaker 1", 30, 35, "...")]
    spans = [("Anna", 0, 10), ("Philip", 11, 20)]

    assert [line.who for line in speakers.put_names(lines, spans)] == [
        "Anna", "Philip", "Anna"]


def test_my_own_name_never_goes_to_their_lines():
    """Teams marks me speaking too, and its marks lag behind the voice."""
    lines = [Line("me", 0, 10, "..."), them(9, 20), Line("me", 21, 30, "...")]
    spans = [("Yaroslav", 0, 10.5), ("Philip", 10.5, 20), ("Yaroslav", 21, 30)]

    assert [line.who for line in speakers.put_names(lines, spans)] == [
        "me", "Philip", "me"]


def test_names_come_even_when_the_voices_could_not_be_told_apart(tmp_path, monkeypatch):
    def broken(status):
        raise RuntimeError("no network")

    monkeypatch.setattr(speakers, "ensure_models", broken)
    folder = said(tmp_path, [[0.0, ["Philip, speaking"]], [10.0, []]])

    out = speakers.relabel([them(1, 9)], folder)

    assert [line.who for line in out] == ["Philip"]
    assert "<v Philip>" in to_vtt(out)
