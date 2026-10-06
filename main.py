"""Folder watcher, job queue and CLI. Produces JSON only — email is always sent
from the UI."""
import argparse
import json
import logging
import queue
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import history
import llm
import paths

EXTENSIONS = {".vtt", ".docx"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(paths.logs() / "pipeline.log", encoding="utf-8"),
              logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("pipeline")

for _line in paths.MIGRATED:            # what the first start brought across
    log.info("Data folder: %s", _line)


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    stamp = datetime.now().strftime("%H%M%S")
    return path.with_name(f"{path.stem}-{stamp}{path.suffix}")


def process_file(src: Path, meeting: str = None, model: str = None,
                 progress_cb=None) -> Path:
    """Summarise one meeting into processed/<name>.json.

    The source file is only ever read — never moved, renamed or rewritten. It is
    the user's own download; the history is what remembers it, so there is nothing
    left to achieve by shuffling it into another folder.
    """
    import parser as tparser

    src = Path(src)
    turns = tparser.parse(src)
    if not turns:
        raise ValueError("no dialogue lines could be extracted")
    meeting = meeting or history.fingerprint(turns)
    known = history.find(meeting)
    log.info("Processing %s (%s)", src.name, meeting[:12])
    history.record(meeting, src, "processing")
    try:
        meta = {"source": src.name,
                "date": datetime.fromtimestamp(src.stat().st_mtime).strftime("%Y-%m-%d")}
        data = llm.summarize(turns, meta, model=model, progress_cb=progress_cb)

        # Summarising a meeting again replaces its own notes rather than leaving a
        # second file next to them.
        out = (Path(known["notes"]) if known and known.get("notes")
               else _unique(paths.processed() / f"{src.stem}.json"))
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

        history.set_status(meeting, "ready", notes=str(out), error=None,
                           model=(data.get("_meta") or {}).get("model", ""))
        log.info("Done: %s (%d turns)", out.name, len(turns))
        return out

    except llm.UNREACHABLE:
        # Ollama is not there. Not a failure: back to found, and the worker holds
        # the job until it answers.
        history.set_status(meeting, "found")
        raise
    except Exception as e:
        log.error("Failed on %s: %s", src.name, e)
        history.set_status(meeting, "failed", error=f"{type(e).__name__}: {e}")
        raise


def register(path, *, force=False, require_transcript=False) -> dict:
    """Parse and fingerprint a file, and decide whether it is a new meeting.

    This is where a duplicate stops: parsing costs milliseconds, the model costs
    minutes. Returns what was decided — ok, queued, hash, status, notes, note —
    for whoever asked to report.
    """
    import parser as tparser

    src = Path(path)
    try:
        turns = tparser.parse(src)
    except Exception as e:
        log.error("Could not read %s: %s", src.name, e)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if not turns:
        log.error("No dialogue lines in %s", src.name)
        return {"ok": False, "error": "no dialogue lines could be extracted"}

    meeting = history.fingerprint(turns)
    known = history.find(meeting)
    if known and not force:
        if known["status"] in history.TERMINAL:
            log.info("Already seen %s as %s (%s)",
                     src.name, known["status"], meeting[:12])
            return {"ok": True, "queued": False, "hash": meeting,
                    "status": known["status"], "notes": known["notes"],
                    "error": known["error"], "note": f"already {known['status']}"}
        # found or processing: queued but never finished, or running right now.
        # Hand it on — the worker drops a duplicate itself — and leave the row
        # alone, so the job that owns it is the only thing writing to it.
        return {"ok": True, "queued": True, "hash": meeting,
                "status": known["status"]}

    if require_transcript and not known:
        ok, reason = tparser.looks_like_transcript(turns)
        if not ok:
            # Recorded so the watcher stops reconsidering it on every start.
            history.record(meeting, src, "skipped", error=reason)
            log.info("Ignored %s: %s", src.name, reason)
            return {"ok": True, "queued": False, "hash": meeting,
                    "status": "skipped", "note": reason}

    history.record(meeting, src, "found")
    return {"ok": True, "queued": True, "hash": meeting, "status": "found"}


def submit(path, *, force=False, require_transcript=False) -> dict:
    """The one way a file becomes a job: through the history, then the queue."""
    decision = register(path, force=force, require_transcript=require_transcript)
    if not decision.get("queued"):
        return decision
    return {**decision, **WORKER.submit(path, meeting=decision["hash"])}


def retry(meeting: str) -> dict:
    """Have another go at a meeting already in the history, from its own row."""
    known = history.find(meeting)
    if not known:
        return {"ok": False, "error": "no such meeting"}
    src = Path(known["source"])
    if not src.exists():
        return {"ok": False, "error": f"the transcript is no longer at {src}"}
    return submit(src, force=True)


# ---------------------------------------------------------------- job queue

@dataclass
class Job:
    """One unit of work: a transcript to process, or a model to download."""
    target: str
    kind: str = "process"            # "process" | "pull"
    meeting: str = None              # the fingerprint, for a transcript


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
        # Called on the worker thread as on_event(kind, target, payload), where
        # kind is done / error / skip / pull. The menu bar app hops to the main
        # thread inside it; the worker knows nothing about any UI.
        self.on_event = None
        self._thread = None
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._waiting = set()        # resolved targets queued or running

    # -------------------------------------------------- public

    def submit(self, target, kind="process", meeting=None) -> dict:
        """Queue a job. Never refuses because the worker is busy."""
        key = self._key(target)
        with self._lock:
            if key in self._waiting:
                return {"ok": True, "queued": False, "note": "already in the queue"}
            self._waiting.add(key)
            if self._thread is None:
                self._stopping.clear()
                self._thread = threading.Thread(target=self._loop, name="worker",
                                                daemon=True)
                self._thread.start()
        self.queue.put(Job(str(target), kind, meeting))
        log.info("Queued %s (%d in the queue)", Path(str(target)).name,
                 self.queue.qsize())
        return {"ok": True, "queued": True}

    def snapshot(self) -> dict:
        """What the worker is doing right now, for /api/job and the menu."""
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

    @staticmethod
    def _key(target) -> str:
        return str(Path(str(target)).expanduser().resolve())

    def _loop(self):
        while True:
            job = self.queue.get()
            if job is None:
                break
            try:
                self._run(job)
            finally:
                with self._lock:
                    self._waiting.discard(self._key(job.target))

    def _run(self, job: Job):
        self.state.update(active=True, stage="starting", i=0, n=0, error="",
                          result=None, note="", file=Path(job.target).name)
        try:
            if job.kind == "pull":
                self._pull(job.target)
            else:
                self._process(job)
        except Exception as e:
            self.state["error"] = str(e)
            self._emit("error", job.target, e)
        finally:
            self.state.update(active=False, stage="")

    def _process(self, job: Job):
        """Summarise one meeting, waiting out an Ollama that is not there."""
        def progress(stage, i, n):
            self.state.update(stage=stage, i=i, n=n)

        while not self._stopping.is_set():
            if not self._wait_for_ollama(job):
                return
            try:
                out = process_file(Path(job.target), meeting=job.meeting,
                                   model=paths.setting("OLLAMA_MODEL") or None,
                                   progress_cb=progress)
            except llm.UNREACHABLE as e:
                # It went away mid-run. Still not a failure — wait and start over.
                log.warning("Ollama unreachable during %s, will retry: %s",
                            Path(job.target).name, e)
                continue
            self.state["result"] = str(out)
            self._emit("done", job.target, out)
            return

    def _wait_for_ollama(self, job: Job) -> bool:
        """Hold a job until Ollama answers. False if the worker is stopping."""
        waited = False
        while not llm.ollama_up():
            if not waited:
                log.info("Waiting for Ollama before %s", Path(job.target).name)
                self.state.update(stage="waiting", note="waiting for Ollama")
                waited = True
            if self._stopping.wait(retry_seconds()):
                return False
        if waited:
            log.info("Ollama is back")
            self.state.update(stage="starting", note="")
        return True

    def _pull(self, model: str):
        def on_pull(status, done, total):
            self.state.update(stage="pull", i=done // 1048576,
                              n=(total or 1) // 1048576, note=status)

        self.state["file"] = model          # a model name, not a file
        llm.pull_model(model, on_pull)
        self.state["note"] = "model downloaded"
        self._emit("pull", model, None)

    def _emit(self, kind: str, target, payload):
        if not self.on_event:
            return
        try:
            self.on_event(kind, target, payload)
        except Exception as e:      # a broken listener must not kill the worker
            log.error("Job listener failed on %s: %s", kind, e)


WORKER = Worker()


def submit_detected(path) -> dict:
    """What the watcher does with a stable file: hand it to the history.

    The transcript check runs, because nobody chose this file by hand — it just
    appeared in the folder.
    """
    return submit(path, require_transcript=True)


# ---------------------------------------------------------------- watcher

def stable_seconds() -> float:
    return paths.setting_float("STABLE_SECONDS", 30)


def poll_seconds() -> float:
    return paths.setting_float("POLL_SECONDS", 5)


def retry_seconds() -> float:
    return paths.setting_float("OLLAMA_RETRY_SECONDS", 15)


def scan_max_age_hours() -> float:
    # What makes it safe to point INBOX_DIR at ~/Downloads: on startup the
    # watcher ignores whatever has been sitting in the folder for longer.
    return paths.setting_float("SCAN_MAX_AGE_HOURS", 24)


class Watcher:
    """Detects stable transcripts in one folder and queues them. Never processes.

    That split is the whole point: this thread only calls stat(), so stopping it
    takes milliseconds and can never be stuck behind a model run. A file is only
    handed on once its size has stopped changing for STABLE_SECONDS, because
    OneDrive makes a file appear immediately and fills it in afterwards, and a
    truncated transcript produces a silently wrong summary.
    """

    def __init__(self, inbox=None, on_file=submit_detected):
        self.inbox = Path(inbox) if inbox else paths.inbox()
        self.on_file = on_file
        self.observer = None
        self.thread = None
        self._stop = threading.Event()
        self._pending = {}           # path -> (size, when the size last changed)

    def start(self):
        """Scan once, then watch. Replaces a running watcher if there is one."""
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        if self.observer or self.thread:
            self.stop()

        self._stop = threading.Event()
        self._pending = {}
        self.inbox.mkdir(parents=True, exist_ok=True)
        self._scan()

        note = self._note

        class Handler(FileSystemEventHandler):
            def on_created(self, e):
                if not e.is_directory:
                    note(e.src_path)

            def on_modified(self, e):
                if not e.is_directory:
                    note(e.src_path)

            def on_moved(self, e):
                if not e.is_directory:
                    note(e.dest_path)

        self.observer = Observer()
        self.observer.schedule(Handler(), str(self.inbox), recursive=False)
        self.observer.start()
        self.thread = threading.Thread(target=self._loop, name="watcher", daemon=True)
        self.thread.start()
        log.info("Watching %s (stability %s s)", self.inbox, stable_seconds())

    def stop(self, timeout: float = 1.0):
        """Release the folder. Returns at once; a running job is left alone."""
        self._stop.set()
        observer, thread = self.observer, self.thread
        self.observer = self.thread = None
        if observer:
            observer.stop()
            observer.join(timeout)
            if observer.is_alive():
                log.warning("Observer on %s did not stop within %s s",
                            self.inbox, timeout)
        if thread:
            thread.join(timeout)
        if observer or thread:
            log.info("Stopped watching %s", self.inbox)

    # -------------------------------------------------- internals

    def _note(self, path):
        p = Path(path)
        if p.suffix.lower() in EXTENSIONS and not p.name.startswith((".", "~")):
            self._pending.setdefault(str(p.resolve()), (-1, time.time()))

    def _scan(self):
        max_age = scan_max_age_hours()
        cutoff = time.time() - max_age * 3600
        skipped = 0
        for f in self.inbox.iterdir():
            if not f.is_file() or f.suffix.lower() not in EXTENSIONS:
                continue
            if f.stat().st_mtime >= cutoff:
                self._note(f)
            else:
                skipped += 1
        if skipped:
            log.info("Ignored %d file(s) older than %g h", skipped, max_age)

    def _loop(self):
        # Wait on the event rather than sleeping, so a stop is acted on at once.
        while not self._stop.wait(poll_seconds()):
            self._sweep()

    def _sweep(self):
        now = time.time()
        stable = stable_seconds()
        for key in list(self._pending):
            p = Path(key)
            try:
                current = p.stat().st_size
            except OSError:              # moved or deleted while we waited
                self._pending.pop(key, None)
                continue
            size, since = self._pending[key]
            if current != size:
                self._pending[key] = (current, now)   # still growing — restart
            elif now - since >= stable:
                self._pending.pop(key, None)
                try:
                    self.on_file(p)
                except Exception as e:   # queueing must not kill the watcher
                    log.error("Could not queue %s: %s", p.name, e)


def main():
    ap = argparse.ArgumentParser(description="Teams transcript -> meeting notes (JSON)")
    ap.add_argument("--once", metavar="FILE", help="process one file and exit")
    ap.add_argument("--parse-only", metavar="FILE",
                    help="show how the transcript parsed, without calling the model")
    ap.add_argument("--model", help="override the Ollama model")
    ap.add_argument("--force", action="store_true",
                    help="summarise again a meeting already in the history")
    args = ap.parse_args()

    if args.parse_only:
        import parser as tparser
        src = Path(args.parse_only).expanduser().resolve()
        if not src.exists():
            sys.exit(f"No such file: {src}")
        turns = tparser.parse(src)
        if not turns:
            sys.exit("No dialogue lines recognised — format not supported")
        ok, reason = tparser.looks_like_transcript(turns)
        text = tparser.to_text(turns)
        meeting = history.fingerprint(turns)
        known = history.find(meeting)
        print(f"Verdict:  {'looks like a transcript' if ok else 'NOT a transcript — ' + reason}")
        print(f"Meeting:  {meeting[:16]}"
              + (f"  (already {known['status']})" if known else "  (new)"))
        print(f"Turns:    {len(turns)}")
        print(f"Speakers: {', '.join(tparser.speakers(turns))}")
        print(f"Size:     {len(text)} chars, ~{len(text) // 3} tokens")
        print(f"Mode:     {'map-reduce' if len(text) // 3 > 6000 else 'single pass'}")
        print("-" * 60)
        for speaker, line in turns[:5]:
            print(f"{speaker}: {line[:160]}{'…' if len(line) > 160 else ''}")
        if len(turns) > 5:
            print(f"... {len(turns) - 5} more turns")
        return

    if args.once:
        src = Path(args.once).expanduser().resolve()
        if not src.exists():
            sys.exit(f"No such file: {src}")

        def show(stage, i, n):
            print(f"  {stage} {i}/{n}", flush=True)

        # The same history as the watcher: a meeting already summarised is not
        # summarised again unless you say so.
        decision = register(src, force=args.force)
        if not decision["ok"]:
            sys.exit(decision["error"])
        if not decision["queued"]:
            print(f"{src.name}: {decision['note']}"
                  + (f"\n{decision['notes']}" if decision.get("notes") else ""))
            print("Add --force to summarise it again.")
            return
        out = process_file(src, meeting=decision["hash"], model=args.model,
                           progress_cb=show)
        print(out.read_text(encoding="utf-8"))
    else:
        # Headless watcher: the same queue and worker the menu bar app uses.
        watcher = Watcher()
        watcher.start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            log.info("Stopping")
        finally:
            watcher.stop()
            WORKER.stop(timeout=1)


if __name__ == "__main__":
    main()
