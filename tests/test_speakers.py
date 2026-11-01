"""Telling the other side's voices apart, and naming them. The voices here are
given as turns; sherpa-onnx itself is only run by hand on real audio."""
import llm
import parser as tparser
import speakers
from transcriber import Line, Live, to_vtt


def them(start, end, text="..."):
    return Line("them", start, end, text)


def test_their_lines_are_numbered_by_voice_in_the_order_they_first_speak():
    lines = [them(0, 4), Line("me", 4, 6, "ok"), them(6, 9), them(10, 12), them(13, 15)]
    turns = [(7, 0.1, 4.2), (3, 6, 9.5), (7, 9.9, 12.1)]     # voice 7 opens the call

    who = [line.who for line in speakers.assign(lines, turns)]

    # The last line overlaps no voice: it keeps the one before it.
    assert who == ["Speaker 1", "me", "Speaker 2", "Speaker 1", "Speaker 1"]


def test_one_voice_on_their_side_stays_them():
    lines = [them(0, 4), them(5, 8)]
    assert speakers.assign(lines, [(0, 0, 4), (0, 5, 8)]) == lines


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
    live = Live(tmp_path, transcribe=lambda s: "x",
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
