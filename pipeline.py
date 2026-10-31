"""Turning a transcript into notes: decide whether it is new, then summarise it.

A file is read once, into a `Transcript`. `register` decides what to do with it,
`process` is the model run, and the `Worker` is the one thread that does model
runs, one at a time. Everything that wants a transcript summarised goes through
`submit`.
"""
import json
import logging
import queue
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import history
import llm
import parser as tparser
import paths

log = logging.getLogger("pipeline")


@dataclass(frozen=True)
class Transcript:
    """One file, read: what was said, and the fingerprint that names the meeting."""
    src: Path
    turns: list
    hash: str
    date: str                       # the file's own date, for the model's context
    named: bool = True              # False when the format names no speakers


def read(path) -> Transcript:
    """Parse a file. Raises when it cannot be read or holds no dialogue."""
    src = Path(path)
    parsed = tparser.parse(src)
    if not parsed.turns:
        raise ValueError("no dialogue lines could be extracted")
    date = datetime.fromtimestamp(src.stat().st_mtime).strftime("%Y-%m-%d")
    return Transcript(src, parsed.turns, history.fingerprint(parsed.turns), date,
                      parsed.named)


@dataclass
class Decision:
    """What became of a file that was offered: queued, or why not."""
    queued: bool
    transcript: Transcript = None   # None when the file could not be read
    status: str = ""                # the meeting's status in the history
    notes: str = ""                 # its notes file, when there is one
    note: str = ""                  # why it was not queued
    error: str = ""                 # why it could not be read

    @property
    def meeting(self) -> str:
        return self.transcript.hash if self.transcript else ""


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    stamp = datetime.now().strftime("%H%M%S")
    return path.with_name(f"{path.stem}-{stamp}{path.suffix}")


def process(t: Transcript, model: str = None, progress_cb=None) -> Path:
    """Summarise one meeting into processed/<name>.json.

    The source file is only ever read — never moved, renamed or rewritten. It is
    the user's own download; the history is what remembers it, so there is nothing
    left to achieve by shuffling it into another folder.
    """
    known = history.find(t.hash)
    log.info("Processing %s (%s)", t.src.name, t.hash[:12])
    history.record(t.hash, t.src, "processing")
    try:
        data = llm.summarize(t.turns, {"source": t.src.name, "date": t.date,
                                       "named": t.named},
                             model=model, progress_cb=progress_cb)

        # Summarising a meeting again replaces its own notes rather than leaving a
        # second file next to them.
        out = (Path(known["notes"]) if known and known.get("notes")
               else _unique(paths.processed() / f"{t.src.stem}.json"))
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

        history.set_status(t.hash, "ready", notes=str(out), error=None,
                           model=(data.get("_meta") or {}).get("model", ""))
        log.info("Done: %s (%d turns)", out.name, len(t.turns))
        return out

    except llm.UNREACHABLE:
        # Ollama is not there. Not a failure: back to found, and the worker holds
        # the job until it answers.
        history.set_status(t.hash, "found")
        raise
    except Exception as e:
        log.error("Failed on %s: %s", t.src.name, e)
        history.set_status(t.hash, "failed", error=f"{type(e).__name__}: {e}")
        raise


def register(path, *, by_user=True, force=False) -> Decision:
    """Read a file and decide whether it is a meeting to summarise.

    This is where a duplicate stops: parsing costs milliseconds, the model costs
    minutes. A meeting with notes is never summarised again unless forced. A
    failed or skipped one is left alone when the watcher sees it again, but tried
    again when someone picks it by hand — that is what picking it means. The
    transcript check only runs on files nobody chose.
    """
    try:
        t = read(path)
    except Exception as e:
        log.error("Could not read %s: %s", Path(path).name, e)
        return Decision(False, error=f"{type(e).__name__}: {e}")

    known = history.find(t.hash)
    status = known["status"] if known else ""
    if status in ("found", "processing"):
        # Queued but never finished, or running right now. Hand it on — the
        # worker drops a duplicate by its hash — and leave the row alone, so the
        # job that owns it is the only thing writing to it.
        return Decision(True, t, status)
    if known and not force and (status in ("ready", "sent") or not by_user):
        log.info("Already seen %s as %s (%s)", t.src.name, status, t.hash[:12])
        return Decision(False, t, status, known["notes"] or "", note=f"already {status}")

    if not by_user and not known:
        ok, reason = tparser.looks_like_transcript(tparser.Parsed(t.turns, t.named))
        if not ok:
            # Recorded so the watcher stops reconsidering it on every start.
            history.record(t.hash, t.src, "skipped", error=reason)
            log.info("Ignored %s: %s", t.src.name, reason)
            return Decision(False, t, "skipped", note=reason)

    history.record(t.hash, t.src, "found")
    return Decision(True, t, "found")


def submit(path, *, by_user=True, force=False) -> Decision:
    """The one way a file becomes a job: through the history, then the queue."""
    decision = register(path, by_user=by_user, force=force)
    if decision.queued and not WORKER.submit(Job(transcript=decision.transcript)):
        decision.queued, decision.note = False, "already in the queue"
    return decision


def submit_detected(path) -> Decision:
    """What the watcher does with a stable file: nobody chose it, so it is checked."""
    return submit(path, by_user=False)


def retry(meeting: str) -> Decision:
    """Have another go at a meeting already in the history, from its own row."""
    known = history.find(meeting)
    if not known:
        return Decision(False, error="no such meeting")
    src = Path(known["source"])
    if not src.exists():
        return Decision(False, error=f"the transcript is no longer at {src}")
    return submit(src, force=True)


def resume() -> int:
    """Queue again whatever a quit or a crash left found or processing.

    Called once at start-up. A transcript that has gone, or changed since, is
    marked failed with the reason rather than left looking busy for ever.
    Returns how many were queued.
    """
    queued = 0
    for row in history.unfinished():
        src = Path(row["source"])
        try:
            t = read(src)
        except Exception as e:
            reason = (f"the transcript is no longer at {src}" if not src.exists()
                      else f"{type(e).__name__}: {e}")
            history.set_status(row["hash"], "failed", error=reason)
            continue
        if t.hash != row["hash"]:
            history.set_status(row["hash"], "failed",
                               error="the transcript changed before it was summarised")
            continue
        queued += WORKER.submit(Job(transcript=t))
    if queued:
        log.info("Resumed %d unfinished meeting(s)", queued)
    return queued


# ---------------------------------------------------------------- job queue

@dataclass
class Job:
    """One unit of work: a meeting to summarise, or a model to download."""
    transcript: Transcript = None
    model: str = ""

    @property
    def key(self) -> str:
        """Two jobs with the same key are the same job, whatever file they came in."""
        return self.transcript.hash if self.transcript else f"model:{self.model}"

    @property
    def label(self) -> str:
        return self.transcript.src.name if self.transcript else self.model


class Worker:
    """The only thing in the app that runs the model, one job at a time.

    Whoever wants a transcript summarised — the watcher, "Process a file…", the
    window — puts a job here, and a single thread takes them in order. Two model
    runs at once swap a 16 GB machine into uselessness, and one queue is also the
    only honest way to show one job state in the UI no matter who started it.

    The worker is deliberately independent of the watcher's lifetime: pausing or
    restarting the watcher never touches a job that is already running.
    """

    def __init__(self):
        self.queue = queue.Queue()
        self.state = {"active": False, "stage": "", "i": 0, "n": 0, "file": "",
                      "error": "", "result": None, "note": ""}
        # Called on the worker thread as on_event(kind, label, payload), where
        # kind is done / error / pull. The menu bar app hops to the main thread
        # inside it; the worker knows nothing about any UI.
        self.on_event = None
        self._thread = None
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._waiting = set()        # keys of the jobs queued or running

    # -------------------------------------------------- public

    def submit(self, job: Job) -> bool:
        """Queue a job. False only if the same job is already queued or running."""
        with self._lock:
            if job.key in self._waiting:
                return False
            self._waiting.add(job.key)
            if self._thread is None:
                self._stopping.clear()
                self._thread = threading.Thread(target=self._loop, name="worker",
                                                daemon=True)
                self._thread.start()
        self.queue.put(job)
        log.info("Queued %s (%d in the queue)", job.label, self.queue.qsize())
        return True

    def snapshot(self) -> dict:
        """What the worker is doing right now, for the window and the menu."""
        with self._lock:
            return dict(self.state, queued=self.queue.qsize())

    def stop(self, timeout: float = None):
        """Let the current job finish, then retire the thread. Quit and tests."""
        self._stopping.set()
        with self._lock:
            thread, self._thread = self._thread, None
        if thread:
            self.queue.put(None)
            thread.join(timeout)

    # -------------------------------------------------- internals

    def _set(self, **values):
        with self._lock:
            self.state.update(values)

    def _loop(self):
        while True:
            job = self.queue.get()
            if job is None:
                break
            try:
                self._run(job)
            finally:
                with self._lock:
                    self._waiting.discard(job.key)

    def _run(self, job: Job):
        self._set(active=True, stage="starting", i=0, n=0, error="", result=None,
                  note="", file=job.label)
        try:
            if job.transcript:
                self._process(job.transcript)
            else:
                self._pull(job.model)
        except Exception as e:
            self._set(error=str(e))
            self._emit("error", job.label, e)
        finally:
            self._set(active=False, stage="")

    def _process(self, t: Transcript):
        """Summarise one meeting, waiting out an Ollama that is not there."""
        def progress(stage, i, n):
            self._set(stage=stage, i=i, n=n)

        while not self._stopping.is_set():
            if not self._wait_for_ollama(t):
                return
            try:
                out = process(t, model=paths.setting("OLLAMA_MODEL") or None,
                              progress_cb=progress)
            except llm.UNREACHABLE as e:
                # It went away mid-run. Still not a failure — wait and start over.
                log.warning("Ollama unreachable during %s, will retry: %s", t.src.name, e)
                continue
            self._set(result=str(out))
            self._emit("done", t.src.name, out)
            return

    def _wait_for_ollama(self, t: Transcript) -> bool:
        """Hold a job until Ollama answers. False if the worker is stopping."""
        waited = False
        while not llm.ollama_up():
            if not waited:
                log.info("Waiting for Ollama before %s", t.src.name)
                self._set(stage="waiting", note="waiting for Ollama")
                waited = True
            if self._stopping.wait(retry_seconds()):
                return False
        if waited:
            log.info("Ollama is back")
            self._set(stage="starting", note="")
        return True

    def _pull(self, model: str):
        def on_pull(status, done, total):
            self._set(stage="pull", i=done // 1048576, n=(total or 1) // 1048576,
                      note=status)

        llm.pull_model(model, on_pull)
        self._set(note="model downloaded")
        self._emit("pull", model, None)

    def _emit(self, kind: str, label: str, payload):
        if not self.on_event:
            return
        try:
            self.on_event(kind, label, payload)
        except Exception as e:      # a broken listener must not kill the worker
            log.error("Job listener failed on %s: %s", kind, e)


WORKER = Worker()


def retry_seconds() -> float:
    return paths.number("OLLAMA_RETRY_SECONDS")
