"""Folder watcher, job queue and CLI. Produces JSON only — email is always sent
from the UI."""
import argparse
import json
import logging
import os
import queue
import shutil
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from paths import DATA, ENV_FILE

load_dotenv(ENV_FILE)

BASE = DATA
INBOX_DIR = Path(os.getenv("INBOX_DIR", DATA / "inbox")).expanduser().resolve()
PROCESSED_DIR = Path(os.getenv("PROCESSED_DIR", DATA / "processed")).expanduser().resolve()
FAILED_DIR = Path(os.getenv("FAILED_DIR", DATA / "failed")).expanduser().resolve()
LOG_DIR = DATA / "logs"
STABLE_SECONDS = int(os.getenv("STABLE_SECONDS", "30"))
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "5"))
# On startup, ignore whatever has been sitting in the folder for a while. This is
# what makes it safe to point INBOX_DIR at ~/Downloads: old files stay untouched.
SCAN_MAX_AGE_HOURS = float(os.getenv("SCAN_MAX_AGE_HOURS", "24"))
EXTENSIONS = {".vtt", ".docx"}

for d in (INBOX_DIR, PROCESSED_DIR, FAILED_DIR, LOG_DIR):
    d.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_DIR / "pipeline.log", encoding="utf-8"),
              logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("pipeline")


class NotATranscript(Exception):
    """The file parsed fine but is not a meeting transcript. Leave it alone."""


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    stamp = datetime.now().strftime("%H%M%S")
    return path.with_name(f"{path.stem}-{stamp}{path.suffix}")


def process_file(src: Path, model: str = None, progress_cb=None,
                 move: bool = True, require_transcript: bool = False) -> Path:
    """Transcript -> processed/<name>.json.

    On failure the source moves to failed/ with a .log beside it and the exception
    is re-raised. A file rejected as "not a transcript" is never moved: it is
    somebody else's document that happened to land in the watched folder.
    """
    import parser as tparser
    from llm import summarize

    src = Path(src)
    log.info("Processing %s", src.name)
    try:
        turns = tparser.parse(src)
        if not turns:
            raise ValueError("no dialogue lines could be extracted")

        if require_transcript:
            ok, reason = tparser.looks_like_transcript(turns)
            if not ok:
                log.info("Skipping %s: %s", src.name, reason)
                raise NotATranscript(reason)

        meta = {"source": src.name,
                "date": datetime.fromtimestamp(src.stat().st_mtime).strftime("%Y-%m-%d")}
        data = summarize(turns, meta, model=model, progress_cb=progress_cb)

        out = _unique(PROCESSED_DIR / f"{src.stem}.json")
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

        if move:
            shutil.move(str(src), str(_unique(PROCESSED_DIR / src.name)))
        log.info("Done: %s (%d turns)", out.name, len(turns))
        return out

    except NotATranscript:
        raise                       # not ours — leave the file where it is
    except Exception as e:
        log.error("Failed on %s: %s", src.name, e)
        if move and src.exists():
            dst = _unique(FAILED_DIR / src.name)
            shutil.move(str(src), str(dst))
            stem = dst.stem
        else:
            stem = src.stem
        (FAILED_DIR / f"{stem}.log").write_text(
            f"{datetime.now().isoformat()}\n{src}\n\n{traceback.format_exc()}",
            encoding="utf-8")
        raise


# ---------------------------------------------------------------- job queue

@dataclass
class Job:
    """One unit of work: a transcript to process, or a model to download."""
    target: str
    kind: str = "process"            # "process" | "pull"
    require_transcript: bool = False
    move: bool = False


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
        self._waiting = set()        # resolved targets queued or running

    # -------------------------------------------------- public

    def submit(self, target, kind="process", require_transcript=False,
               move=False) -> dict:
        """Queue a job. Never refuses because the worker is busy."""
        key = self._key(target)
        with self._lock:
            if key in self._waiting:
                return {"ok": True, "queued": False, "note": "already in the queue"}
            self._waiting.add(key)
            if self._thread is None:
                self._thread = threading.Thread(target=self._loop, name="worker",
                                                daemon=True)
                self._thread.start()
        self.queue.put(Job(str(target), kind, require_transcript, move))
        log.info("Queued %s (%d in the queue)", Path(str(target)).name,
                 self.queue.qsize())
        return {"ok": True, "queued": True}

    def snapshot(self) -> dict:
        """What the worker is doing right now, for /api/job and the menu."""
        return dict(self.state, queued=self.queue.qsize())

    def stop(self, timeout: float = None):
        """Let the current job finish, then retire the thread. Quit and tests."""
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
                def progress(stage, i, n):
                    self.state.update(stage=stage, i=i, n=n)

                out = process_file(Path(job.target),
                                   model=os.getenv("OLLAMA_MODEL") or None,
                                   progress_cb=progress, move=job.move,
                                   require_transcript=job.require_transcript)
                self.state["result"] = str(out)
                self._emit("done", job.target, out)
        except NotATranscript as e:
            self.state["note"] = str(e)
            self._emit("skip", job.target, e)
        except Exception as e:
            self.state["error"] = str(e)
            self._emit("error", job.target, e)
        finally:
            self.state.update(active=False, stage="")

    def _pull(self, model: str):
        import llm

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
    """What the watcher does with a stable file: hand it to the worker.

    The transcript check runs, because nobody chose this file by hand — it just
    appeared in the folder.
    """
    return WORKER.submit(path, require_transcript=True, move=True)


# ---------------------------------------------------------------- watcher

class Watcher:
    """Detects stable transcripts in one folder and queues them. Never processes.

    That split is the whole point: this thread only calls stat(), so stopping it
    takes milliseconds and can never be stuck behind a model run. A file is only
    handed on once its size has stopped changing for STABLE_SECONDS, because
    OneDrive makes a file appear immediately and fills it in afterwards, and a
    truncated transcript produces a silently wrong summary.
    """

    def __init__(self, inbox, on_file=submit_detected):
        self.inbox = Path(inbox)
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
        log.info("Watching %s (stability %s s)", self.inbox, STABLE_SECONDS)

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
        cutoff = time.time() - SCAN_MAX_AGE_HOURS * 3600
        skipped = 0
        for f in self.inbox.iterdir():
            if not f.is_file() or f.suffix.lower() not in EXTENSIONS:
                continue
            if f.stat().st_mtime >= cutoff:
                self._note(f)
            else:
                skipped += 1
        if skipped:
            log.info("Ignored %d file(s) older than %g h", skipped, SCAN_MAX_AGE_HOURS)

    def _loop(self):
        # Wait on the event rather than sleeping, so a stop is acted on at once.
        while not self._stop.wait(POLL_SECONDS):
            self._sweep()

    def _sweep(self):
        now = time.time()
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
            elif now - since >= STABLE_SECONDS:
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
        print(f"Verdict:  {'looks like a transcript' if ok else 'NOT a transcript — ' + reason}")
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
        # A file outside INBOX_DIR is a one-off run — do not relocate it.
        move = INBOX_DIR in src.parents

        def show(stage, i, n):
            print(f"  {stage} {i}/{n}", flush=True)

        out = process_file(src, model=args.model, progress_cb=show, move=move)
        print(out.read_text(encoding="utf-8"))
    else:
        # Headless watcher: the same queue and worker the menu bar app uses.
        watcher = Watcher(INBOX_DIR)
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
