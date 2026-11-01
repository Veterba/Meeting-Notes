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


def ask_for_file() -> str:
    return osa('POSIX path of (choose file with prompt "Choose a transcript" '
               'of type {"vtt", "docx"})')


def ask_for_folder() -> str:
    return osa('POSIX path of (choose folder with prompt "Where transcripts arrive")')


class Controller:
    def __init__(self):
        self.watcher = None
        # What Ollama said, last time it was asked. Filled in off the main thread,
        # because asking it is a network call and no UI must wait for it.
        self.net = {"ollama_up": False, "model": llm.default_model(),
                    "model_ready": False, "models": [], "installed": []}
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
        if not llm.ollama_up():
            if llm.start_ollama():
                log.info("Started Ollama")
            else:
                self._notice("Ollama is not installed", "Install it with: brew install ollama")
        self.probe()

    def probe(self):
        """Ask Ollama what it is and what it has. Never on the main thread."""
        up = llm.ollama_up()
        installed = llm.installed_models() if up else []
        model = llm.default_model()
        net = {"ollama_up": up, "model": model, "models": llm.model_choices(installed),
               "installed": installed, "model_ready": llm.has_model(model, installed)}
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

    def set_model(self, name: str):
        """Use a model, downloading it first through the worker if it is missing."""
        if not self.net.get("ollama_up"):
            raise ValueError("Ollama is not running, so no model can be chosen")
        paths.save_setting("OLLAMA_MODEL", name)
        if name not in (self.net.get("installed") or []):
            pipeline.WORKER.submit(pipeline.Job(model=name))
            self._notice("Downloading the model", f"{name} — several GB, this takes a while")
        threading.Thread(target=self.probe, daemon=True).start()
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

    def submit(self, path) -> pipeline.Decision:
        """A file chosen by hand: the transcript check is skipped, and a meeting
        already summarised is not summarised again."""
        decision = pipeline.submit(path)
        self._changed()
        return decision

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
