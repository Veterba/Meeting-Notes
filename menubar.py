"""macOS menu bar app. Owns the watcher, the job queue, the local API and the window.

Run: python menubar.py  (or ./run.sh, or the built Meeting Notes.app)
No Dock icon. The dropdown is the app: status at the top, then the meetings it has
seen and what can be done to them, then the settings. The window opens for one
meeting, to read its notes and send them.
"""
import json
import subprocess
import threading
from pathlib import Path

import rumps
from PyObjCTools import AppHelper

import history
import llm
import mailer
import main as pipeline
import menu
import paths

ICON = paths.CODE / "assets" / "icon.png"
ICON_PAUSED = paths.CODE / "assets" / "icon-paused.png"


def on_main(fn, *args):
    """Run fn on the main thread.

    The menu, the icon and the notifications are all AppKit, and AppKit from a
    background thread freezes or crashes the app. Callbacks arrive here from the
    worker thread and from the Ollama probe, so every one of them goes through
    this and nothing else touches the UI.
    """
    if threading.current_thread() is threading.main_thread():
        fn(*args)
    else:
        AppHelper.callAfter(fn, *args)


def osa(script: str) -> str:
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


# The three buttons of the mail account sheet, in the order they are added.
SAVE, CANCEL, TEST = 1000, 1001, 1002


def mail_account_sheet(address: str, secret: str, status: str = "") -> tuple:
    """The small sheet: an address, an app password, and a way to try them.

    rumps' own window holds one field, so this is an NSAlert with two of them in
    its accessory view. Returns (which button, address, password).
    """
    from AppKit import (NSAlert, NSMakeRect, NSSecureTextField, NSTextField, NSView)

    alert = NSAlert.alloc().init()
    alert.setMessageText_("Mail account")
    alert.setInformativeText_(status or (
        "The address the notes are sent from, and its app password. The password "
        "goes into your Keychain, never into a file.\n\n"
        "Gmail: myaccount.google.com/apppasswords"))
    for title in ("Save", "Cancel", "Send test email"):
        alert.addButtonWithTitle_(title)

    view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 320, 52))
    email = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 28, 320, 22))
    email.setStringValue_(address or "")
    email.setPlaceholderString_("you@gmail.com")
    field = NSSecureTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 320, 22))
    field.setStringValue_(secret or "")
    field.setPlaceholderString_("app password")
    view.addSubview_(email)
    view.addSubview_(field)
    alert.setAccessoryView_(view)
    alert.window().setInitialFirstResponder_(email)

    clicked = alert.runModal()
    return clicked, str(email.stringValue()), str(field.stringValue())


def notify(title: str, message: str):
    try:
        rumps.notification(title, "", message)
    except Exception:  # unbundled runs have no bundle id for notifications
        osa(f"display notification {message!r} with title {title!r}")


class TranscriptApp(rumps.App):
    def __init__(self):
        super().__init__("Meeting Notes", icon=str(ICON), template=True, quit_button=None)
        self.watcher = None
        self.enabled = paths.flag("WATCH_ENABLED")
        self.status_items = []
        self._offered_keychain = False
        # What Ollama said, last time it was asked. Filled in off the main thread,
        # because asking it is a network call and the menu must not wait for it.
        self.net = {"ollama_up": False, "model": llm.default_model(),
                    "model_ready": False, "models": [], "installed": []}
        pipeline.WORKER.on_event = self.on_job_event

        import server
        import window
        url, _token, self.httpd = server.serve()
        self.window = window.AppWindow(url)

        self.rebuild()
        self.apply_watch_state()
        threading.Thread(target=self._startup_checks, daemon=True).start()

    # ------------------------------------------------------------ the menu

    def status_facts(self) -> dict:
        """What the four status lines need. Cheap enough to ask every two seconds."""
        return {**self.net, "watching": self.enabled, "folder": paths.inbox(),
                "job": pipeline.WORKER.snapshot()}

    def facts(self) -> dict:
        return {**self.status_facts(), "mail_mode": mailer.mail_mode(),
                "meetings": history.listing(7)}

    def rebuild(self):
        """Draw the whole dropdown from menu.tree(). Main thread only."""
        self.status_items = []
        items = []
        for item in menu.tree(self.facts()):
            drawn = self.draw(item)
            if item.role == "status":
                self.status_items.append(drawn)
            items.append(drawn)
        self.menu.clear()
        self.menu = items

    def draw(self, item):
        """One menu.Item, and everything under it, as rumps objects."""
        if menu.is_separator(item):
            return None
        kwargs = {"key": item.key} if item.key else {}
        if item.action:
            method = getattr(self, item.action)
            kwargs["callback"] = lambda sender, m=method, a=item.arg: m(sender, a)
        drawn = rumps.MenuItem(item.title, **kwargs)
        if item.checked is not None:
            drawn.state = 1 if item.checked else 0
        for child in item.children:
            drawn.add(self.draw(child))
        return drawn

    @rumps.timer(2)
    def tick(self, _):
        """Keep the status lines current while a job runs. No network, no sqlite."""
        for line, item in zip(menu.status_lines(self.status_facts()), self.status_items):
            item.title = line
        if not self._offered_keychain:
            self._offered_keychain = True     # before the modal, not after
            self.offer_the_keychain()

    def offer_the_keychain(self):
        """Once: an app password left in .env from before the Keychain was used."""
        if not (mailer.password_in_env() and mailer.account()):
            return
        if paths.flag("KEYCHAIN_OFFERED", False):
            return
        move = rumps.alert(
            title="Move the mail password out of .env?",
            message=(f"The app password for {mailer.account()} is sitting in plain "
                     "text in .env. It can live in your Keychain instead, and be "
                     "taken out of the file."),
            ok="Move it", cancel="Leave it")
        if not move:
            paths.save_setting("KEYCHAIN_OFFERED", "1")
            return
        problem = mailer.move_password_to_keychain()
        if problem:
            notify("Could not use the Keychain", problem)
            return                            # worth offering again next time
        paths.save_setting("KEYCHAIN_OFFERED", "1")
        notify("Password moved", "It is in your Keychain now, and out of .env.")

    @rumps.timer(20)
    def probe_timer(self, _):
        threading.Thread(target=self._probe, daemon=True).start()

    def _probe(self):
        """Ask Ollama what it is and what it has. Never on the main thread."""
        up = llm.ollama_up()
        installed = llm.installed_models() if up else []
        model = llm.default_model()
        choices = []
        for name in [model, llm.MODEL_SMALL, llm.MODEL_MID, llm.MODEL_BIG] + installed:
            if name and name not in choices:
                choices.append(name)
        on_main(self._probed, {
            "ollama_up": up, "model": model, "models": choices, "installed": installed,
            "model_ready": model in installed or f"{model}:latest" in installed})

    def _probed(self, net: dict):
        # Only redraw the whole thing when the choices themselves changed; the
        # status lines are updated by the two-second tick anyway.
        redraw = (net["models"] != self.net["models"] or net["model"] != self.net["model"])
        self.net = net
        self.rebuild() if redraw else self.tick(None)

    def _startup_checks(self):
        """The slow questions, off the main thread: the Keychain and Ollama."""
        pipeline.log.info("Keychain: %s",
                          "available" if mailer.keychain_ready() else "NOT available")
        if not llm.ollama_up():
            if llm.start_ollama():
                pipeline.log.info("Started Ollama")
            else:
                on_main(notify, "Ollama is not installed",
                        "Install it with: brew install ollama")
        self._probe()

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

        self.icon = str(ICON if self.enabled else ICON_PAUSED)
        self.template = True

        if not self.enabled:
            pipeline.log.info("Watching paused")
            return

        self.watcher = pipeline.Watcher()      # the folder comes from paths
        self.watcher.start()

    # ------------------------------------------------------------ jobs

    def on_job_event(self, kind, target, payload):
        """The worker finished something, on the worker thread."""
        on_main(self._job_event, kind, target, payload)

    def _job_event(self, kind, target, payload):
        name = Path(str(target)).name
        if kind == "done":
            out = Path(payload)
            try:
                title = json.loads(out.read_text(encoding="utf-8")).get("title") or out.stem
            except Exception:
                title = out.stem
            notify("Notes ready", f"{title} — pick it from the menu to read and send")
        elif kind == "error":
            notify("Could not process", f"{name}: {payload}")
        elif kind == "pull":
            notify("Model downloaded", name)
        self.rebuild()

    # ------------------------------------------------------------ actions

    def pick_file(self, sender, _arg=None):
        path = osa('POSIX path of (choose file with prompt "Choose a transcript" '
                   'of type {"vtt", "docx"})')
        if not path:
            return
        # Chosen by hand, so the transcript check is skipped. Queued, not
        # refused, if the worker is busy — and if this meeting has been
        # summarised before, its notes open instead of being made again.
        decision = pipeline.submit(path)
        if not decision.get("ok"):
            notify("Could not read that file", decision.get("error", ""))
        elif not decision.get("queued") and decision.get("notes"):
            self.window.show(open_path=decision["notes"])
        self.rebuild()

    def toggle_watching(self, sender, _arg=None):
        self.enabled = not self.enabled
        paths.save_setting("WATCH_ENABLED", "1" if self.enabled else "0")
        self.apply_watch_state()
        self.rebuild()

    def open_notes(self, sender, meeting):
        row = history.find(meeting)
        if row and row["notes"]:
            self.window.show(open_path=row["notes"])

    def reprocess(self, sender, meeting):
        result = pipeline.retry(meeting)
        if not result.get("ok"):
            notify("Cannot reprocess", result.get("error", ""))
        self.rebuild()

    def reveal(self, sender, meeting):
        row = history.find(meeting)
        if not row:
            return
        src = Path(row["source"])
        if src.exists():
            subprocess.run(["open", "-R", str(src)], check=False)
        else:
            notify("The transcript has moved", f"It is no longer at {src}")

    def choose_folder(self, sender, _arg=None):
        path = osa('POSIX path of (choose folder with prompt '
                   '"Where transcripts arrive")')
        if not path:
            return
        paths.save_setting("INBOX_DIR", path.rstrip("/"))
        self.apply_watch_state()
        self.rebuild()

    def choose_model(self, sender, name):
        paths.save_setting("OLLAMA_MODEL", name)
        if name not in (self.net.get("installed") or []):
            pipeline.WORKER.submit(name, kind="pull")
            notify("Downloading the model", f"{name} — several GB, this takes a while")
        threading.Thread(target=self._probe, daemon=True).start()
        self.rebuild()

    def choose_mail(self, sender, mode):
        paths.save_setting("MAIL_MODE", mode)
        self.rebuild()

    def mail_account(self, sender, _arg=None):
        """The address and the app password, and a way to prove they work."""
        address, secret, status = mailer.account(), "", ""
        while True:
            clicked, address, secret = mail_account_sheet(address, secret, status)
            if clicked == CANCEL:
                return
            if clicked == TEST:
                # Blocking, but the sheet is modal anyway; the send has a 20 s
                # timeout so this cannot hang the menu for longer than that.
                ok, message = mailer.send_test(address, secret or None)
                status = ("✓ " if ok else "✕ ") + message
                continue
            problem = mailer.save_account(address, secret)
            if problem:
                status = "✕ " + problem
                continue
            notify("Mail account saved", f"Sending as {address.strip()}")
            self.rebuild()
            return

    def edit_recipients(self, sender, _arg=None):
        answer = rumps.Window(
            "Everyone on this list gets every set of notes. One per line: a bare "
            'address, or "Name <address>".',
            "Default recipients",
            default_text="\n".join(mailer.split_addresses(paths.setting("RECIPIENTS"))),
            ok="Save", cancel="Cancel", dimensions=(300, 90)).run()
        if answer.clicked:
            entries = [line.strip() for line in answer.text.splitlines() if line.strip()]
            paths.save_setting("RECIPIENTS", ", ".join(entries))

    def quit(self, sender, _arg=None):
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
