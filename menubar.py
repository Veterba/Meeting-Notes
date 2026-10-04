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

import llm
import main as pipeline

from paths import CODE, DATA, ENV_FILE

BASE = CODE
ENV = ENV_FILE
ICON = CODE / "assets" / "icon.png"
ICON_PAUSED = CODE / "assets" / "icon-paused.png"


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
        self.stop_event = None
        self.worker = None
        self.enabled = env_flag("WATCH_ENABLED")

        import server
        import window
        self.server_mod = server
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
            notify("Ollama is not installed",
                   "Install it with: brew install ollama")
        self.refresh(None)

    # ------------------------------------------------------------ watcher

    def apply_watch_state(self):
        """Stop whatever is running, then start again only if watching is on.

        Also called after the folder or the model changes, so the running watcher
        always matches the settings.
        """
        if self.stop_event:
            self.stop_event.set()
        if self.worker:
            self.worker.join(timeout=5)   # let the observer release the folder
        self.stop_event = None
        self.worker = None

        self.toggle.state = 1 if self.enabled else 0
        self.icon = str(ICON if self.enabled else ICON_PAUSED)
        self.template = True

        if not self.enabled:
            pipeline.log.info("Watching paused")
            return

        self.stop_event = threading.Event()
        inbox = Path(os.getenv("INBOX_DIR", DATA / "inbox")).expanduser().resolve()
        pipeline.INBOX_DIR = inbox
        self.worker = threading.Thread(
            target=pipeline.watch,
            kwargs={"inbox": inbox, "model": os.getenv("OLLAMA_MODEL") or None,
                    "on_done": self.on_done, "on_error": self.on_error,
                    "on_skip": self.on_skip, "stop_event": self.stop_event},
            daemon=True)
        self.worker.start()

    def toggle_watching(self, _):
        self.enabled = not self.enabled
        save_setting("WATCH_ENABLED", "1" if self.enabled else "0")
        self.apply_watch_state()
        self.refresh(None)

    def on_settings_changed(self, changed: dict):
        """The window saved settings; bring this process in line with them."""
        if "WATCH_ENABLED" in changed:
            self.enabled = env_flag("WATCH_ENABLED")
        self.apply_watch_state()
        self.refresh(None)

    def on_done(self, src, out):
        try:
            title = json.loads(Path(out).read_text(encoding="utf-8")).get("title", out.stem)
        except Exception:
            title = Path(out).stem
        notify("Notes ready", f"{title} — click the icon to review and send")
        self.rebuild_history()

    def on_error(self, src, exc):
        notify("Could not process", f"{Path(src).name}: {exc}")

    def on_skip(self, src, exc):
        # Not a transcript, just a file sharing the folder. Log only — a
        # notification per downloaded document would be noise.
        pipeline.log.info("Ignored %s: %s", Path(src).name, exc)

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
        self.server_mod.start_job(Path(path))
        self.window.show()

    def quit(self, _):
        if self.stop_event:
            self.stop_event.set()
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
