"""What the app can be asked to do, whoever asks: the menu bar or the window.

Both front ends call these and redraw from `facts()`; neither keeps its own copy
of a setting, and neither owns the watcher. `on_change` and `on_notice` are
called on whatever thread made the change — the menu bar hops to the main thread
inside them, the window simply polls.
"""
import subprocess
import threading
from pathlib import Path

import history
import llm
import mailer
import paths
import pipeline
from menu import MAIL_MODES
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
        self._lock = threading.Lock()

    # ------------------------------------------------------------ facts

    @property
    def watching(self) -> bool:
        return paths.flag("WATCH_ENABLED")

    def status_facts(self) -> dict:
        """Cheap enough to ask every two seconds: no network, no sqlite."""
        return {**self.net, "watching": self.watching, "folder": paths.inbox(),
                "job": pipeline.WORKER.snapshot()}

    def facts(self, meetings: int = 7) -> dict:
        return {**self.status_facts(), "mail_mode": mailer.mail_mode(),
                "meetings": history.listing(meetings)}

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
        threading.Thread(target=self._startup_checks, daemon=True).start()

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

    def save_account(self, address: str, secret: str) -> str:
        """Save the mail account. Returns what is wrong with it, or ""."""
        problem = mailer.save_account(address, secret)
        if not problem:
            self._changed()
        return problem

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
