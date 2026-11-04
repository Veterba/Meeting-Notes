"""Watch one folder for transcripts that have finished arriving, and hand them on."""
import logging
import threading
import time
from pathlib import Path

import parser as tparser
import paths
import pipeline

log = logging.getLogger("pipeline")


def stable_seconds() -> float:
    return paths.number("STABLE_SECONDS")


def poll_seconds() -> float:
    return paths.number("POLL_SECONDS")


def scan_max_age_hours() -> float:
    # What makes it safe to point INBOX_DIR at ~/Downloads: on startup the
    # watcher ignores whatever has been sitting in the folder for longer.
    return paths.number("SCAN_MAX_AGE_HOURS")


class Watcher:
    """Detects stable transcripts in one folder and queues them. Never processes.

    That split is the whole point: this thread only calls stat(), so stopping it
    takes milliseconds and can never be stuck behind a model run. A file is only
    handed on once its size has stopped changing for STABLE_SECONDS, because
    OneDrive makes a file appear immediately and fills it in afterwards, and a
    truncated transcript produces a silently wrong summary.
    """

    def __init__(self, inbox=None, on_file=pipeline.submit_detected):
        self.inbox = Path(inbox) if inbox else paths.inbox()
        self.on_file = on_file
        self.observer = None
        self.thread = None
        self._stop = threading.Event()
        # path -> (size, when the size last changed). Written by the observer's
        # thread and read by this one's, hence the lock.
        self._pending = {}
        self._lock = threading.Lock()

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
        self.thread = threading.Thread(target=self._loop, args=(self._stop,),
                                       name="watcher", daemon=True)
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
        if tparser.is_candidate(p):
            with self._lock:
                self._pending.setdefault(str(p.resolve()), (-1, time.time()))

    def _scan(self):
        max_age = scan_max_age_hours()
        cutoff = time.time() - max_age * 3600
        skipped = 0
        for f in self.inbox.iterdir():
            if not f.is_file() or not tparser.is_candidate(f):
                continue
            if f.stat().st_mtime >= cutoff:
                self._note(f)
            else:
                skipped += 1
        if skipped:
            log.info("Ignored %d file(s) older than %g h", skipped, max_age)

    def _loop(self, stop: threading.Event):
        # Wait on the event rather than sleeping, so a stop is acted on at once.
        # It is this run's own event: after a restart, a loop still finishing a
        # sweep sees its own stop, not the next run's, and ends.
        while not stop.wait(poll_seconds()):
            self._sweep()

    def _sweep(self):
        """Hand on every file whose size has held still long enough.

        Decided under the lock, handed on outside it: parsing a file takes a
        while, and the observer thread must not wait for that to note the next.
        """
        now, stable, ready = time.time(), stable_seconds(), []
        with self._lock:
            for key, (size, since) in list(self._pending.items()):
                try:
                    current = Path(key).stat().st_size
                except OSError:              # moved or deleted while we waited
                    del self._pending[key]
                    continue
                if current != size:
                    self._pending[key] = (current, now)   # still growing — restart
                elif now - since >= stable:
                    del self._pending[key]
                    ready.append(Path(key))
        for p in ready:
            try:
                self.on_file(p)
            except Exception as e:           # queueing must not kill the watcher
                log.error("Could not queue %s: %s", p.name, e)
