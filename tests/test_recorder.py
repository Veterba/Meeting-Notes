"""Recording, without recording anything: the Swift helper is replaced by a small
script that speaks its protocol — JSON lines on stdout, stop on end of input.
"""
import json
import sys
import textwrap

import pytest

import controller
import paths
from helpers import wait_until
from recorder import Recorder
from transcriber import Live

FAKE = textwrap.dedent('''\
    #!{python}
    import json, sys, time
    folder = sys.argv[1]
    say = lambda **e: print(json.dumps(e), flush=True)
    if "{mode}" == "fail":
        say(event="error", what="microphone permission was not given", status=-1)
        sys.exit(1)
    say(event="started", system={{"rate": 48000, "channels": 1}},
        mic={{"rate": 44100, "channels": 1}}, at=time.time())
    if "{mode}" == "deadmic":
        for _ in range(6):
            say(event="level", system=0.5, mic=0.0, system_frames=16000, mic_frames=16000)
    say(event="level", system=0.5, mic=0.25)
    for _ in sys.stdin:
        pass
    open(folder + "/system.wav", "wb").close()
    open(folder + "/mic.wav", "wb").close()
    say(event="stopped", seconds=1.5)
''')


@pytest.fixture
def helper(tmp_path, monkeypatch):
    """Install the fake helper; returns a function to pick how it behaves."""
    def install(mode="ok"):
        script = tmp_path / "mn-recorder"
        script.write_text(FAKE.format(python=sys.executable, mode=mode))
        script.chmod(0o755)
        monkeypatch.setattr(paths, "recorder_binary", lambda: script)
    monkeypatch.setattr(paths, "DATA", tmp_path / "data")
    # No whisper either: the transcriber hears every piece as the same line.
    monkeypatch.setattr(Recorder, "make_live",
                        staticmethod(lambda folder: Live(folder, transcribe=lambda s, p: "hello")))
    install()
    return install


def test_a_recording_is_a_folder_with_both_streams_and_their_timing(helper):
    events = []
    rec = Recorder()
    rec.on_event = lambda kind, folder, message: events.append(kind)

    folder = rec.start()
    assert wait_until(lambda: rec.snapshot()["mic"] == 0.25)
    assert rec.recording and rec.snapshot()["recording"]

    rec.stop()

    assert wait_until(lambda: events == ["started", "transcribed"])
    assert not rec.snapshot()["recording"] and not rec.snapshot()["transcribing"]
    assert sorted(p.name for p in folder.iterdir()) == ["meta.json", "mic.wav", "system.wav"]
    meta = json.loads((folder / "meta.json").read_text())
    assert meta["system"]["rate"] == 48000 and meta["mic"]["rate"] == 44100
    assert folder.parent == paths.recordings()


def test_a_refused_permission_is_reported_not_swallowed(helper):
    helper("fail")
    events = []
    rec = Recorder()
    rec.on_event = lambda kind, folder, message: events.append((kind, message))

    rec.start()

    assert wait_until(lambda: events == [("error", "microphone permission was not given")])
    assert rec.snapshot()["error"] == "microphone permission was not given"
    assert not rec.recording


def test_a_microphone_sending_pure_silence_is_reported(helper):
    helper("deadmic")
    events = []
    rec = Recorder()
    rec.on_event = lambda kind, folder, message: events.append((kind, message))

    rec.start()

    assert wait_until(lambda: ("warning", "the microphone is sending pure silence") in events)
    assert rec.snapshot()["warning"] == "the microphone is sending pure silence"
    rec.stop()


def test_one_recording_at_a_time_and_a_missing_helper_says_so(helper, tmp_path, monkeypatch):
    rec = Recorder()
    rec.start()
    with pytest.raises(RuntimeError, match="already recording"):
        rec.start()
    rec.stop()

    monkeypatch.setattr(paths, "recorder_binary", lambda: tmp_path / "nowhere")
    with pytest.raises(RuntimeError, match="not built"):
        Recorder().start()


def test_the_controller_starts_and_stops_it_and_says_when_it_is_saved(helper):
    app = controller.Controller()
    notices = []
    app.on_notice = lambda title, message: notices.append(title)

    app.set_recording(True)
    assert wait_until(lambda: app.status_facts()["recording"]["recording"])
    app.set_recording(False)

    # The fake helper records silence, so there is nothing to summarise.
    assert wait_until(lambda: notices == ["Recording saved"])
