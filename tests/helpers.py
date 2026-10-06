"""Shared bits for the suite. Not collected — it holds no tests."""
import threading
import time
from pathlib import Path

from watchdog.observers.api import BaseObserver

SPEECH = [
    ("Ivan Petrov", "Right, let us start with the release date, everything else hangs on it."),
    ("Maria Sokolova", "Documentation is on track for Friday, the payment history part is done."),
    ("Ivan Petrov", "Good. Then the pilot starts on Monday, the thirtieth of September."),
    ("Maria Sokolova", "Agreed. I will warn support before Thursday and send them the notes."),
    ("Ivan Petrov", "We also need the client letter template, that one is yours as well."),
    ("Maria Sokolova", "I will do both and write in the channel as soon as they are ready."),
]

NOTES = {"title": "", "date": "2026-10-06", "participants": [], "summary": "s",
         "decisions": [], "action_items": [], "assumptions": [], "open_questions": []}


def write_transcript(folder, name, topic=None) -> Path:
    """A transcript that parses and passes the is-it-a-transcript check.

    `topic` is folded into the first line, so two files with different names are
    two different meetings — and so two different fingerprints — unless they are
    deliberately given the same topic.
    """
    speech = list(SPEECH)
    speech[0] = (speech[0][0],
                 f"{speech[0][1]} Today it is about {topic or Path(name).stem}.")
    blocks = ["WEBVTT\n"]
    for i, (who, text) in enumerate(speech, 1):
        blocks.append(f"\n{i}\n00:00:{i * 6:02d}.000 --> 00:00:{i * 6 + 5:02d}.000\n"
                      f"{who}: {text}\n")
    p = Path(folder) / name
    p.write_text("".join(blocks), encoding="utf-8")
    return p


def fake_summarize(turns, meta, model=None, progress_cb=None):
    if progress_cb:
        progress_cb("reduce", 1, 1)
    return dict(NOTES, title=meta["source"], participants=[t[0] for t in turns],
                _meta={"model": "fake-model"})


def wait_until(condition, timeout=15.0, step=0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(step)
    return False


def live_observers() -> list:
    return [t for t in threading.enumerate()
            if isinstance(t, BaseObserver) and t.is_alive()]
