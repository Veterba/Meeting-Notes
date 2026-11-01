"""Noticing a call, without a microphone: the helper's watch mode is replaced by a
script that prints which apps hold the microphone, and the controller's offers
are driven straight from those changes. Nothing here records.
"""
import json
import sys
import textwrap

import pytest

import calls
import controller
import menu
import paths
from helpers import wait_until
from recorder import Recorder

FAKE = textwrap.dedent('''\
    #!{python}
    import json, sys
    assert sys.argv[1] == "--watch"
    for apps in {steps}:
        print(json.dumps({{"event": "mic", "apps": apps}}), flush=True)
    for _ in sys.stdin:
        pass
''')


def test_bundle_ids_become_the_call_apps_it_knows():
    assert calls.call_apps(["com.microsoft.teams2.helper", "us.zoom.xos"]) == {"Teams", "Zoom"}
    assert calls.call_apps(["com.apple.WebKit.GPU"]) == {"Safari"}
    assert calls.call_apps(["ru.keepcoder.Telegram", "local.meetingnotes.app"]) == set()


def test_the_watch_reports_what_started_and_what_ended(tmp_path, monkeypatch):
    steps = [[], ["com.microsoft.teams2.helper", "com.apple.Music"],
             ["com.microsoft.teams2.helper"], []]
    script = tmp_path / "mn-recorder"
    script.write_text(FAKE.format(python=sys.executable, steps=json.dumps(steps)))
    script.chmod(0o755)
    monkeypatch.setattr(paths, "recorder_binary", lambda: script)
    seen = []
    watch = calls.CallWatch(lambda started, ended: seen.append((started, ended)))
    watch.start()
    try:
        assert wait_until(lambda: len(seen) == 2)
    finally:
        watch.stop()
    # Music is not a call app, so dropping it changes nothing.
    assert seen == [({"Teams"}, set()), (set(), {"Teams"})]


def test_no_helper_means_no_watch(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "recorder_binary", lambda: tmp_path / "missing")
    watch = calls.CallWatch(lambda *a: None)
    watch.start()
    assert watch.proc is None


# ------------------------------------------------------------ the offers

@pytest.fixture
def app(monkeypatch):
    recording = {"on": False}
    monkeypatch.setattr(Recorder, "recording", property(lambda self: recording["on"]))
    monkeypatch.setattr(Recorder, "start", lambda self: recording.update(on=True))
    monkeypatch.setattr(Recorder, "stop", lambda self: recording.update(on=False))
    a = controller.Controller()
    a.offers = []
    a.on_offer = lambda offer, name: a.offers.append((offer, name))

    def mic(*apps):
        """The call apps on the microphone now, as the watch would report it."""
        now = set(apps)
        started, ended = now - a.calls.using, a.calls.using - now
        a.calls.using = now
        a._calls_changed(started, ended)
    a.mic = mic
    return a


def test_a_call_starting_offers_to_record_it_and_never_records(app):
    app.mic("Zoom")
    assert app.call == {"offer": "record", "app": "Zoom"}
    assert app.offers == [("record", "Zoom")]
    assert not app.recorder.recording


def test_the_offer_goes_when_the_call_ends_unrecorded(app):
    app.mic("Zoom")
    app.mic()
    assert app.call["offer"] == ""


def test_a_second_app_does_not_offer_again(app):
    app.mic("Teams")
    app.mic("Teams", "Chrome")
    assert app.offers == [("record", "Teams")]


def test_recording_takes_the_offer_and_the_call_ending_offers_to_stop(app):
    app.mic("Teams")
    app.set_recording(True)
    assert app.call["offer"] == ""
    app.mic()
    assert app.call == {"offer": "stop", "app": "Teams"}
    assert app.recorder.recording            # only an offer: still recording
    app.set_recording(False)
    assert app.call["offer"] == ""


def test_the_stop_offer_waits_until_every_call_app_is_done(app):
    app.set_recording(True)
    app.mic("Teams", "Chrome")
    app.mic("Chrome")
    assert app.call["offer"] == ""
    app.mic()
    assert app.call == {"offer": "stop", "app": "Chrome"}


def test_the_microphone_coming_back_takes_the_stop_offer_away(app):
    app.set_recording(True)
    app.mic("Zoom")
    app.mic()
    app.mic("Zoom")
    assert app.call["offer"] == ""


def test_an_offer_can_be_dismissed(app):
    app.mic("Zoom")
    app.dismiss_call()
    assert app.call["offer"] == ""


def test_turning_detection_off_is_saved_and_clears_the_offer(app, temp_env, monkeypatch):
    monkeypatch.setattr(paths, "recorder_binary", lambda: paths.DATA / "missing")
    app.mic("Zoom")
    app.set_call_detect(False)
    assert not paths.flag("CALL_DETECT")
    assert app.call["offer"] == ""
    assert app.status_facts()["call_detect"] is False


def test_the_menu_names_the_call_on_offer():
    assert menu.record_item({}, {"offer": "record", "app": "Zoom"}).title == \
        "● Record the Zoom call"
    assert menu.record_item({}, {"offer": "", "app": ""}).title == "● Record the call"
