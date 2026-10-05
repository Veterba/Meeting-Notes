"""macOS menu bar app. Owns the watcher, the local API and the window.

Run: python menubar.py  (or ./run.sh, or the built Meeting Notes.app)
No Dock icon — the status bar item is the entry point, and the window behind it
holds the editor and the settings.
"""
import json
import os
import subprocess
import threading
from pathlib import Path

import rumps
from dotenv import load_dotenv, set_key
from PyObjCTools import AppHelper

import llm
import main as pipeline

from paths import CODE, DATA, ENV_FILE

BASE = CODE
ENV = ENV_FILE
ICON = CODE / "assets" / "icon.png"
ICON_PAUSED = CODE / "assets" / "icon-paused.png"


def on_main(fn, *args):
    """Run fn on the main thread.

    The menu, the icon and the notifications are all AppKit, and AppKit from a
    background thread freezes or crashes the app. Callbacks arrive here from the
    worker thread, the watcher thread and the HTTP handler threads, so every one
    of them goes through this and nothing else touches the UI.
    """
    if threading.current_thread() is threading.main_thread():
        fn(*args)
    else:
        AppHelper.callAfter(fn, *args)


def env_flag(key: str, default: bool = True) -> bool:
    return os.getenv(key, "1" if default else "0").strip().lower() \
        not in ("0", "false", "no", "off", "")


def save_setting(key: str, value: str):
    """Write to .env and refresh this process's environment.

    One source of settings for the window, the menu and the CLI alike.
    """
    ENV.touch(exist_ok=True)
    set_key(str(ENV), key, value, quote_mode="auto")
    os.environ[key] = value
    load_dotenv(ENV, override=True)


def osa(script: str) -> str:
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def notify(title: str, message: str):
    try:
        rumps.notification(title, "", message)
    except Exception:  # unbundled runs have no bundle id for notifications
        osa(f"display notification {json.dumps(message)} with title {json.dumps(title)}")


def short_path(p) -> str:
    p = str(p)
    home = str(Path.home())
    return "~" + p[len(home):] if p.startswith(home) else p


class TranscriptApp(rumps.App):
    def __init__(self):
        super().__init__("Meeting Notes", icon=str(ICON), template=True, quit_button=None)
        self.watcher = None
        self.enabled = env_flag("WATCH_ENABLED")
        pipeline.WORKER.on_event = self.on_job_event

        import server
        import window
        url, _token, self.httpd = server.serve()
        self.window = window.AppWindow(url)
        server.on_settings_changed = self.on_settings_changed

        self.toggle = rumps.MenuItem("Watch the folder", callback=self.toggle_watching)
        self.st_ollama = rumps.MenuItem("Ollama: checking…")
        self.st_model = rumps.MenuItem("Model: —")
        self.st_folder = rumps.MenuItem("Folder: —")

        self.menu = [
            self.st_ollama, self.st_model, self.st_folder,
            None,
            rumps.MenuItem("Open Meeting Notes", callback=self.open_window, key="o"),
            rumps.MenuItem("Process a file…", callback=self.pick_file),
            self.toggle,
            None,
            self.history_menu(),
            None,
            rumps.MenuItem("Quit", callback=self.quit, key="q"),
        ]
        self.refresh(None)
        self.apply_watch_state()
        # Ollama may not be running yet; bring it up without blocking the menu.
        threading.Thread(target=self._ensure_ollama, daemon=True).start()

    # ------------------------------------------------------------ menu

    def history_menu(self):
        menu = rumps.MenuItem("Recent notes")
        files = sorted(pipeline.PROCESSED_DIR.glob("*.json"),
                       key=lambda f: f.stat().st_mtime, reverse=True)[:7]
        if not files:
            menu.add(rumps.MenuItem("nothing yet"))
            return menu
        for f in files:
            try:
                title = json.loads(f.read_text(encoding="utf-8")).get("title") or f.stem
            except Exception:
                title = f.stem
            item = rumps.MenuItem(title[:48], callback=self.open_note)
            item._path = str(f)
            menu.add(item)
        return menu

    def rebuild_history(self):
        self.menu["Recent notes"].clear()
        for item in self.history_menu().values():
            self.menu["Recent notes"].add(item)

    @rumps.timer(20)
    def refresh(self, _):
        up = llm.ollama_up()
        model = llm.default_model()
        self.st_ollama.title = f"{'●' if up else '○'} Ollama: " + (
            "running" if up else "not responding")
        self.st_model.title = f"Model: {model}" + (
            "" if llm.has_model(model) else "  (not downloaded)")
        self.st_folder.title = (f"Folder: {short_path(pipeline.INBOX_DIR)}"
                                if self.enabled else "Folder: not being watched")

    def _ensure_ollama(self):
        if llm.ollama_up():
            return
        if llm.start_ollama():
            pipeline.log.info("Started Ollama")
        else:
            on_main(notify, "Ollama is not installed",
                    "Install it with: brew install ollama")
        on_main(self.refresh, None)

    # ------------------------------------------------------------ watcher

    def apply_watch_state(self):
        """Match the running watcher to the settings. Main thread only.

        Returns in milliseconds: the watcher only detects files, so stopping it
        never waits for anything. A transcript already being summarised is left
        alone and finishes even if watching is switched off underneath it — the
        worker does not belong to the watcher.
        """
        if self.watcher:
            self.watcher.stop()
            self.watcher = None

        self.toggle.state = 1 if self.enabled else 0
        self.icon = str(ICON if self.enabled else ICON_PAUSED)
        self.template = True

        if not self.enabled:
            pipeline.log.info("Watching paused")
            return

        inbox = Path(os.getenv("INBOX_DIR", DATA / "inbox")).expanduser().resolve()
        pipeline.INBOX_DIR = inbox
        self.watcher = pipeline.Watcher(inbox)
        self.watcher.start()

    def toggle_watching(self, _):
        self.enabled = not self.enabled
        save_setting("WATCH_ENABLED", "1" if self.enabled else "0")
        self.apply_watch_state()
        self.refresh(None)

    def on_settings_changed(self, changed: dict):
        """The window saved settings (on an HTTP thread); come into line."""
        on_main(self._settings_changed, changed)

    def _settings_changed(self, changed: dict):
        if "WATCH_ENABLED" in changed:
            self.enabled = env_flag("WATCH_ENABLED")
        self.apply_watch_state()
        self.refresh(None)

    # ------------------------------------------------------------ jobs

    def on_job_event(self, kind, target, payload):
        """The worker finished something, on the worker thread."""
        on_main(self._job_event, kind, target, payload)

    def _job_event(self, kind, target, payload):
        name = Path(str(target)).name
        if kind == "done":
            out = Path(payload)
            try:
                title = json.loads(out.read_text(encoding="utf-8")).get("title", out.stem)
            except Exception:
                title = out.stem
            notify("Notes ready", f"{title} — click the icon to review and send")
            self.rebuild_history()
        elif kind == "error":
            notify("Could not process", f"{name}: {payload}")
        elif kind == "skip":
            # Not a transcript, just a file sharing the folder. Log only — a
            # notification per downloaded document would be noise.
            pipeline.log.info("Ignored %s: %s", name, payload)

    # ------------------------------------------------------------ actions

    def open_window(self, _):
        self.window.show()

    def open_note(self, item):
        self.window.show(open_path=getattr(item, "_path", None))

    def pick_file(self, _):
        path = osa('POSIX path of (choose file with prompt "Choose a transcript" '
                   'of type {"vtt", "docx"})')
        if not path:
            return
        # Chosen by hand, so the transcript check is skipped and the file stays
        # where it is. Queued, not refused, if the worker is already busy.
        pipeline.WORKER.submit(path)
        self.window.show()

    def quit(self, _):
        if self.watcher:
            self.watcher.stop()
        try:
            self.httpd.shutdown()
        except Exception:
            pass
        rumps.quit_application()


def run():
    from AppKit import NSApplication, NSApplicationActivationPolicyAccessory

    NSApplication.sharedApplication().setActivationPolicy_(
        NSApplicationActivationPolicyAccessory)   # keep it out of the Dock
    TranscriptApp().run()


if __name__ == "__main__":
    run()
