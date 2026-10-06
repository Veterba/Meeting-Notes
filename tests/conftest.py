"""Keep the tests away from the real data folder.

paths.DATA is read when paths is imported, so MEETING_NOTES_DATA has to be set
before any test imports anything. A .env is written into that folder too, so the
first-start migration has nothing to copy and never touches the real settings.

Folders and timings for one test are set with monkeypatch.setenv, which
paths.setting reads ahead of .env.
"""
import os
import tempfile
from pathlib import Path

_DATA = Path(tempfile.mkdtemp(prefix="meeting-notes-tests-"))
(_DATA / ".env").write_text("WATCH_ENABLED=0\n", encoding="utf-8")
os.environ["MEETING_NOTES_DATA"] = str(_DATA)
os.environ["INBOX_DIR"] = str(_DATA / "inbox")      # never the real one

from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402

import main  # noqa: E402
import paths  # noqa: E402
from helpers import write_transcript  # noqa: E402


@pytest.fixture
def transcript():
    return write_transcript


@pytest.fixture
def box(tmp_path, monkeypatch):
    """Temp folders for everything, and a watcher that reacts within a tick."""
    ns = SimpleNamespace(inbox=tmp_path / "inbox", processed=tmp_path / "processed")
    # A data folder of this test's own, so history.db starts empty every time.
    monkeypatch.setattr(paths, "DATA", tmp_path)
    for key, d in vars(ns).items():
        monkeypatch.setenv(f"{key.upper()}_DIR", str(d))
        d.mkdir()
    monkeypatch.setenv("STABLE_SECONDS", "0")
    monkeypatch.setenv("POLL_SECONDS", "0.05")
    return ns


@pytest.fixture
def worker(monkeypatch):
    """A worker of this test's own, in place of the module-wide one.

    main.submit reads main.WORKER when it is called, so patching the module
    attribute leaves the real wiring in place.
    """
    w = main.Worker()
    monkeypatch.setattr(main, "WORKER", w)
    yield w
    w.stop(timeout=15)
