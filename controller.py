"""What the app can be asked to do, whoever asks: the menu bar or the window.

Both front ends call these and redraw from `facts()`; neither keeps its own copy
of a setting, and neither owns the watcher. `on_change` and `on_notice` are
called on whatever thread made the change — the menu bar hops to the main thread
inside them, the window simply polls.
"""
import subprocess
import threading
import time
from pathlib import Path

import archive
import history
import llm
import mailer
import parser as tparser
import paths
import pipeline
import speakers
from calls import CallWatch
from menu import MAIL_MODES
from recorder import Recorder
from transcriber import Live
from watcher import Watcher

log = pipeline.log


def osa(script: str) -> str:
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def ask_for_files() -> list:
    types = ", ".join(f'"{ext[1:]}"' for ext in sorted(tparser.EXTENSIONS))
    return osa(f'set picked to choose file with prompt "Choose transcripts" of type {{{types}}} '
               'with multiple selections allowed\n'
               'set out to ""\n'
               'repeat with f in picked\n'
               '  set out to out & POSIX path of f & linefeed\n'
               'end repeat\n'
               'return out').splitlines()


def ask_for_folder(prompt: str = "Where transcripts arrive") -> str:
    return osa(f'POSIX path of (choose folder with prompt "{prompt}")')


class Controller:
    def __init__(self):
        self.watcher = None
        # What Ollama said, last time it was asked. Filled in off the main thread,
        # because asking it is a network call and no UI must wait for it.
        self.net = {"ollama_up": False, "ollama_installed": True, "model": llm.MODEL,
                    "model_ready": False}
        self.on_change = None           # on_change()
        self.on_notice = None           # on_notice(title, message)
        self.on_offer = None            # on_offer(offer, app): "record" or "stop"
        self.recorder = Recorder()
        self.recorder.on_event = self._recording_event
        self.calls = CallWatch(self._calls_changed)
        # What the app offers about a call it noticed: "record" while not
        # recording, "stop" when the call app let go of the microphone.
        self.call = {"offer": "", "app": ""}
        self.retranscribing = {}        # recording name -> what it is doing
        # The last files brought in by hand, file by file: {"at", "results"}.
        self.imported = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------ facts

    @property
    def watching(self) -> bool:
        return paths.flag("WATCH_ENABLED")

    def status_facts(self) -> dict:
        """Cheap enough to ask every two seconds: no network, no sqlite."""
        return {**self.net, "watching": self.watching, "folder": paths.inbox(),
                "job": pipeline.WORKER.snapshot(), "recording": self.recorder.snapshot(),
                "call": dict(self.call), "call_detect": paths.flag("CALL_DETECT")}

    def facts(self, meetings: int = 7) -> dict:
        return {**self.status_facts(), "mail_mode": mailer.mail_mode(),
                "meetings": history.listing(meetings)}

    def lone_recordings(self) -> list:
        """Recordings that never became a meeting: nothing was recognised in
        them, or they are still being transcribed. Newest first."""
        known = {archive.recording_of(s) for s in history.sources()}
        current = self.recorder.snapshot()["folder"]
        lone = []
        for folder in sorted(paths.recordings().iterdir(), reverse=True):
            if (not folder.is_dir() or folder in known or str(folder) == current
                    or not archive.RE_RECORDING.match(folder.name)):
                continue
            lone.append({"name": folder.name, "title": f"Call {folder.name}",
                         "date": folder.name[:10],
                         "status": self.retranscribing.get(folder.name, "")})
        return lone

    def _changed(self):
        self._call(self.on_change)

    def _notice(self, title: str, message: str):
        self._call(self.on_notice, title, message)

    def _call(self, fn, *args):
        if not fn:
            return
        try:
            fn(*args)
        except Exception as e:      # a broken listener must not break the action
            log.error("Listener failed: %s", e)

    # ------------------------------------------------------------ start-up

    def start(self):
        self.apply_watch_state()
        if paths.flag("CALL_DETECT"):
            self.calls.start()
        threading.Thread(target=self._startup_checks, daemon=True).start()
        threading.Thread(target=self._purge_loop, daemon=True).start()

    def _purge_loop(self):
        """Empty what is due from the archive at start-up, then every six hours."""
        while True:
            try:
                gone = archive.purge()
                if gone:
                    log.info("Removed %d item(s) from the archive for good", gone)
                    self._changed()
            except Exception as e:
                log.error("Could not empty the archive: %s", e)
            time.sleep(6 * 3600)

    def _startup_checks(self):
        """The slow work: what was left unfinished, the Keychain and Ollama."""
        pipeline.resume()
        log.info("Keychain: %s", "available" if mailer.keychain_ready() else "NOT available")
        # Ollama itself is woken by the worker when a meeting needs it.
        if not llm.ollama_binary():
            self._notice("Ollama is not installed", "Install it with: brew install ollama")
        self.probe()

    def probe(self):
        """Ask Ollama whether it is up, and the disk whether the model is there.
        Down is normal: Ollama sleeps until a meeting needs it. Never on the
        main thread."""
        net = {"ollama_up": llm.ollama_up(), "ollama_installed": bool(llm.ollama_binary()),
               "model": llm.MODEL, "model_ready": llm.model_on_disk(llm.MODEL)}
        changed, self.net = net != self.net, net
        if changed:
            self._changed()

    def stop(self):
        self.calls.stop()
        self.recorder.stop()
        with self._lock:
            if self.watcher:
                self.watcher.stop()
                self.watcher = None
        llm.OLLAMA.release()

    # ------------------------------------------------------------ watcher

    def apply_watch_state(self):
        """Match the running watcher to the settings.

        Returns in milliseconds: the watcher only detects files, so stopping it
        never waits for anything, and a job already running belongs to the worker.
        """
        with self._lock:
            if self.watcher:
                self.watcher.stop()
                self.watcher = None
            if not self.watching:
                log.info("Watching paused")
                return
            self.watcher = Watcher()            # the folder comes from paths
            self.watcher.start()

    def set_watching(self, on: bool):
        paths.save_setting("WATCH_ENABLED", "1" if on else "0")
        self.apply_watch_state()
        self._changed()

    def set_folder(self, path: str):
        paths.save_setting("INBOX_DIR", path.rstrip("/"))
        self.apply_watch_state()
        self._changed()

    # ------------------------------------------------------------ settings

    def download_model(self):
        """Download the model through the worker, if it is missing."""
        if not self.net.get("ollama_installed"):
            raise ValueError("Ollama is not installed. Install it with: brew install ollama")
        if not self.net.get("model_ready"):
            pipeline.WORKER.submit(pipeline.Job(model=llm.MODEL))
            self._notice("Downloading the model", f"{llm.MODEL} — several GB, this takes a while")
        self._changed()

    def set_mail_mode(self, mode: str):
        if mode not in dict(MAIL_MODES):
            raise ValueError(f"unknown mail mode {mode}")
        paths.save_setting("MAIL_MODE", mode)
        self._changed()

    def set_recipients(self, entries: list):
        entries = [e.strip() for e in entries if e.strip()]
        paths.save_setting("RECIPIENTS", ", ".join(entries))
        self._changed()

    def set_call_detect(self, on: bool):
        paths.save_setting("CALL_DETECT", "1" if on else "0")
        self.calls.stop()
        if on:
            self.calls.start()
        self._offer("", "")

    def set_archive_days(self, value):
        if int(value) not in archive.CHOICES:
            raise ValueError(f"keep the archive {', '.join(map(str, archive.CHOICES))} days")
        paths.save_setting("ARCHIVE_DAYS", str(int(value)))
        self._changed()

    def save_account(self, address: str, secret: str) -> str:
        """Save the mail account. Returns what is wrong with it, or ""."""
        problem = mailer.save_account(address, secret)
        if not problem:
            self._changed()
        return problem

    # ------------------------------------------------------------ recording

    def set_recording(self, on: bool):
        """Start or stop recording the call. Raises RuntimeError when it cannot start."""
        if on and not self.recorder.recording:
            self.recorder.start()
        elif not on:
            self.recorder.stop()
        self._offer("", "")

    # A call app taking or letting go of the microphone. Only ever an offer:
    # recording starts and stops on the user's click.

    def _calls_changed(self, started: set, ended: set):
        offer, app = self.call["offer"], self.call["app"]
        if self.recorder.recording:
            if ended and not self.calls.using:
                self._offer("stop", min(ended))
            elif started and offer == "stop":
                self._offer("", "")         # it was only switching devices
        elif started and offer != "record":
            self._offer("record", min(started))
        elif offer == "record" and app in ended:
            self._offer("", "")             # the call ended without being recorded

    def _offer(self, offer: str, app: str):
        self.call = {"offer": offer, "app": app}
        if offer:
            log.info("Call: %s offered for %s", offer, app)
            self._call(self.on_offer, offer, app)
        self._changed()

    def dismiss_call(self):
        self._offer("", "")

    def _recording_event(self, kind, folder, message):
        if kind == "error":
            self.call = {"offer": "", "app": ""}
            self._notice("Recording failed", message)
        elif kind == "warning":
            self._notice("Recording problem", message)
        elif kind == "transcribed" and message:
            # The call is a transcript now, and goes the way every transcript goes.
            decision = pipeline.submit(message)
            self._notice("Call transcribed", "Writing the notes now" if decision.queued
                         else decision.error or decision.note)
        elif kind == "transcribed":
            self._notice("Recording saved", "Nothing was said, so there is nothing to summarise")
        self._changed()

    # ------------------------------------------------------------ meetings

    def import_files(self, chosen) -> dict:
        """Files and folders brought in by hand: picked, or dropped on the window.

        A file chosen on its own is summarised whatever it looks like: choosing
        it is the check. The files inside a chosen folder were not looked at one
        by one, so they get the watcher's transcript check, but not its age limit.
        A meeting already summarised is never summarised again.
        """
        results = []
        for path in map(Path, chosen):
            if path.is_dir():
                results += [self._bring(f, by_user=False) for f in tparser.files_in(path)]
            else:
                results.append(self._bring(path, by_user=True))
        return self._report(results)

    def recheck_skipped(self) -> dict:
        """Judge the files the transcript check turned down again, with the
        parser as it is now. A file that has gone is forgotten."""
        results = []
        for row in history.skipped():
            history.forget(row["hash"])
            src = Path(row["source"])
            if src.exists():
                results.append(self._bring(src, by_user=False))
        return self._report(results)

    @staticmethod
    def _bring(path: Path, by_user: bool) -> dict:
        d = pipeline.submit(path, by_user=by_user)
        outcome = ("error" if d.error else "queued" if d.queued
                   else "skipped" if d.status == "skipped" else "seen")
        return {"name": path.name, "meeting": d.meeting, "outcome": outcome,
                "note": d.error or d.note, "notes": d.notes}

    def _report(self, results: list) -> dict:
        self.imported = {"at": time.time(), "results": results}
        self._changed()
        return self.imported

    def retry(self, meeting: str) -> pipeline.Decision:
        decision = pipeline.retry(meeting)
        self._changed()
        return decision

    # ------------------------------------------------------------ deleting

    def archive_meeting(self, meeting: str):
        archive.archive_meeting(meeting)
        self._changed()

    def archive_recording(self, name: str):
        if self.recorder.snapshot()["folder"].endswith(name) or name in self.retranscribing:
            raise RuntimeError("that recording is in use")
        archive.archive_recording(name)
        self._changed()

    def restore(self, entry_id: str):
        archive.restore(entry_id)
        self._changed()

    # ------------------------------------------------------------ transcribing again

    def retranscribe(self, name: str):
        """Run Whisper over a whole recording again, in the background, and
        summarise what comes out. For a call whose live transcript went wrong."""
        folder = archive.recording(name)
        if self.recorder.recording or self.retranscribing:
            raise RuntimeError("one transcription at a time — finish the recording first")
        self.retranscribing[name] = "transcribing"
        threading.Thread(target=self._retranscribe, args=(folder,), daemon=True).start()
        self._changed()

    def _retranscribe(self, folder):
        old = self._meetings_in(folder)
        try:
            transcript = Live(folder, relabel=speakers.relabel).finish_all()
            if not transcript:
                self._notice("Nothing recognised", f"No speech was found in Call {folder.name}")
                return
            decision = pipeline.submit(transcript, force=True)
            # The old transcript's meeting gives way to the new one.
            for h in old:
                if h != decision.meeting:
                    row = history.find(h)
                    if row and row["notes"] and Path(row["notes"]).exists():
                        Path(row["notes"]).unlink()
                    history.set_status(h, "archived")
            self._notice("Transcribed again", "Writing the notes now" if decision.queued
                         else decision.error or decision.note)
        except Exception as e:
            log.error("Transcribing %s again failed: %s", folder.name, e)
            self._notice("Could not transcribe", str(e))
        finally:
            self.retranscribing.pop(folder.name, None)
            self._changed()

    @staticmethod
    def _meetings_in(folder) -> list:
        return [r["hash"] for r in history.recent(10000)
                if archive.recording_of(r["source"]) == folder]

    def reveal(self, meeting: str) -> str:
        """Show the transcript in Finder. Returns why it could not, or ""."""
        row = history.find(meeting)
        if not row:
            return "no such meeting"
        src = Path(row["source"])
        if not src.exists():
            return f"The transcript is no longer at {src}"
        subprocess.run(["open", "-R", str(src)], check=False)
        return ""
