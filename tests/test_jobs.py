"""Detecting a file and processing it are separate, and only one job runs at a time.

No Ollama: llm.summarize is replaced by a fake that records when it ran and, where
a test needs a job to still be running, blocks on an event until the test releases
it. Blocking on an event rather than sleeping for five seconds keeps the suite fast
and makes "the restart happened while the model was busy" a fact instead of a race.

Everything the pipeline writes goes under tmp_path, and no folder outside it is
ever watched.
"""
import threading
import time

import pytest

import llm
import main
from helpers import NOTES, live_observers, wait_until


def all_done(worker, rec, n) -> bool:
    """Both jobs really over: the fake returned, the notes are written, idle again.

    rec.finished is appended inside the fake, which still leaves process_file to
    write the JSON and the worker to emit its event — so the worker's own state is
    what says the queue is empty.
    """
    job = worker.snapshot()
    return len(rec.finished) == n and not job["active"] and not job["queued"]


class Recorder:
    """Stands in for the model. Counts how many runs overlap, and in what order."""

    def __init__(self):
        self.started, self.finished = [], []
        self.live = self.max_live = 0
        self.gate = None             # set to an Event to hold the job open
        self.entered = threading.Event()
        self._lock = threading.Lock()

    def summarize(self, turns, meta, model=None, progress_cb=None):
        with self._lock:
            self.live += 1
            self.max_live = max(self.max_live, self.live)
            self.started.append(meta["source"])
        self.entered.set()
        try:
            if progress_cb:
                progress_cb("reduce", 1, 1)
            if self.gate:
                assert self.gate.wait(15), "the test never released the job"
            else:
                time.sleep(0.05)     # long enough for an overlap to be visible
            return dict(NOTES, title=meta["source"], participants=[t[0] for t in turns])
        finally:
            with self._lock:
                self.live -= 1
                self.finished.append(meta["source"])


@pytest.fixture
def rec(monkeypatch):
    r = Recorder()
    monkeypatch.setattr(llm, "summarize", r.summarize)
    monkeypatch.setattr(llm, "ollama_up", lambda: True)   # never ask the real one
    return r


@pytest.fixture
def watcher(box, worker):
    w = main.Watcher()                   # the folder comes from paths
    yield w
    w.stop()


def test_restarting_the_watcher_does_not_wait_for_a_running_job(
        box, rec, worker, watcher, transcript):
    rec.gate = threading.Event()
    transcript(box.inbox, "first.vtt")
    watcher.start()
    assert rec.entered.wait(15), "the dropped file was never processed"
    first_observer = watcher.observer

    started = time.monotonic()
    watcher.start()                       # what changing a setting does
    restart = time.monotonic() - started

    assert restart < 1.0, f"the restart took {restart:.1f} s"
    assert not first_observer.is_alive()
    assert live_observers() == [watcher.observer], "more than one observer"
    assert rec.live == 1, "the running job was disturbed by the restart"

    transcript(box.inbox, "second.vtt")   # dropped after the restart
    rec.gate.set()

    assert wait_until(lambda: all_done(worker, rec, 2)), f"only did {rec.finished}"
    assert rec.started == ["first.vtt", "second.vtt"]
    assert rec.max_live == 1
    assert (box.processed / "first.json").exists()
    assert (box.processed / "second.json").exists()


def test_two_files_dropped_together_run_one_after_the_other(
        box, rec, worker, watcher, transcript):
    events = []
    worker.on_event = lambda kind, target, payload: events.append((kind, str(target)))

    transcript(box.inbox, "a.vtt")
    transcript(box.inbox, "b.vtt")
    watcher.start()

    assert wait_until(lambda: all_done(worker, rec, 2)), f"only did {rec.finished}"
    assert rec.max_live == 1, "two jobs overlapped"
    assert rec.started == rec.finished, "a job started before the one before it ended"
    assert sorted(rec.finished) == ["a.vtt", "b.vtt"]
    # What the menu bar app listens to, so it can notify and refresh its history.
    assert [k for k, _ in events] == ["done", "done"]


def test_a_hand_picked_job_waits_instead_of_being_refused(box, rec, worker, transcript):
    rec.gate = threading.Event()
    first = transcript(box.inbox, "first.vtt")
    second = transcript(box.inbox, "second.vtt")

    assert worker.submit(first)["queued"] is True
    assert rec.entered.wait(15), "the first job never started"

    queued = worker.submit(second)        # the worker is busy right now
    assert queued == {"ok": True, "queued": True}
    assert worker.snapshot()["queued"] == 1
    assert worker.snapshot()["active"] is True
    assert rec.started == ["first.vtt"], "the second job started too early"

    rec.gate.set()
    assert wait_until(lambda: all_done(worker, rec, 2)), f"only did {rec.finished}"
    assert rec.started == ["first.vtt", "second.vtt"]
    assert rec.max_live == 1
    # Picked by hand: the files stay where they are.
    assert first.exists() and second.exists()
