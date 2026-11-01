"""Deleting a meeting or a recording: into the archive, back out of it, and gone
after the chosen number of days. Nothing here records or runs a model."""
import time

import pytest

import archive
import controller
import history
import llm
import paths
import pipeline
from helpers import fake_summarize, wait_until, write_transcript


@pytest.fixture
def summarised(box, worker, monkeypatch):
    """Summarise a transcript; returns (meeting hash, the transcript's path)."""
    monkeypatch.setattr(llm, "summarize", fake_summarize)
    monkeypatch.setattr(llm, "ollama_up", lambda: True)

    def make(folder, name):
        src = write_transcript(folder, name)
        decision = pipeline.submit(src)
        assert wait_until(lambda: history.find(decision.meeting)["status"] == "ready")
        return decision.meeting, src
    return make


def recorded_call(name="2026-11-01 10-00-00"):
    folder = paths.recordings() / name
    folder.mkdir(parents=True)
    (folder / "mic.wav").write_bytes(b"RIFF")
    (folder / "system.wav").write_bytes(b"RIFF")
    return folder


def test_a_downloaded_meeting_leaves_its_transcript_alone(box, summarised):
    meeting, src = summarised(box.inbox, "standup.vtt")
    notes = history.find(meeting)["notes"]

    entry = archive.archive_meeting(meeting)

    assert src.exists()                             # the user's own file stays put
    assert not (box.processed / notes.split("/")[-1]).exists()
    assert history.find(meeting)["status"] == "archived"
    assert meeting not in [m["hash"] for m in history.listing()]
    assert [e["id"] for e in archive.entries()] == [entry]
    # The watcher seeing the same transcript again does not bring it back.
    assert not pipeline.submit_detected(src).queued


def test_a_recorded_meeting_goes_with_its_recording_and_comes_back(box, summarised):
    folder = recorded_call()
    meeting, _ = summarised(folder, "Call 2026-11-01 10-00-00.vtt")

    entry = archive.archive_meeting(meeting)
    assert not folder.exists()

    archive.restore(entry)
    assert (folder / "mic.wav").exists()
    assert history.find(meeting)["status"] == "ready"
    assert archive.entries() == []


def test_a_recording_with_nothing_in_it_can_be_deleted_too(box):
    app = controller.Controller()
    folder = recorded_call()
    assert [r["name"] for r in app.lone_recordings()] == [folder.name]

    app.archive_recording(folder.name)
    assert app.lone_recordings() == [] and not folder.exists()
    with pytest.raises(KeyError):
        app.archive_recording("../../etc")


def test_the_archive_empties_itself_after_the_chosen_days(box, temp_env, summarised):
    meeting, _ = summarised(box.inbox, "standup.vtt")
    archive.archive_meeting(meeting)
    paths.save_setting("ARCHIVE_DAYS", "7")

    assert archive.purge(now=time.time() + 6 * 86400) == 0
    assert archive.purge(now=time.time() + 8 * 86400) == 1
    assert archive.entries() == []
    # The history keeps the row, so the transcript is still known.
    assert history.find(meeting)["status"] == "archived"


def test_only_a_week_a_month_or_three_months(box, temp_env):
    app = controller.Controller()
    app.set_archive_days(90)
    assert archive.days() == 90
    with pytest.raises(ValueError):
        app.set_archive_days(1)


def test_a_call_is_transcribed_again_and_its_old_meeting_gives_way(box, summarised,
                                                                   monkeypatch):
    folder = recorded_call()
    old, _ = summarised(folder, "Call 2026-11-01 10-00-00.vtt")

    class Whisper:                      # a second, better transcription
        def __init__(self, folder, **_):
            self.folder = folder

        def finish_all(self):
            return write_transcript(self.folder, "Call 2026-11-01 10-00-00.vtt",
                                    topic="the release, heard properly")
    monkeypatch.setattr(controller, "Live", Whisper)
    app = controller.Controller()

    app.retranscribe(folder.name)

    assert wait_until(lambda: not app.retranscribing)
    assert history.find(old)["status"] == "archived"
    fresh = [m for m in history.listing() if m["hash"] != old]
    assert len(fresh) == 1
    assert wait_until(lambda: history.find(fresh[0]["hash"])["status"] == "ready")
