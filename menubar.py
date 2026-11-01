"""The macOS app: the window, the menu bar widget, the local API behind them.

Run: python menubar.py  (or ./run.sh, or the built Meeting Notes.app)
The window is the app: every meeting, its notes, the settings. The menu bar
dropdown is the quick view of the same thing — status at the top, recent
meetings, the settings — and keeps the app running when the window is closed.
Both act through one `Controller`.
"""
import json
import subprocess
import threading
from pathlib import Path

import rumps
from PyObjCTools import AppHelper

import mailer
import main
import menu
import paths
import pipeline
from controller import Controller, ask_for_file, ask_for_folder

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
        # The text goes in as arguments, so no quote in a file name can break it.
        subprocess.run(["osascript", "-e", "on run argv",
                        "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
                        "-e", "end run", title, message], capture_output=True)


class TranscriptApp(rumps.App):
    def __init__(self):
        super().__init__("Meeting Notes", icon=str(ICON), template=True, quit_button=None)
        self.status_items = []
        self.record_item = None
        self.app = Controller()
        self.app.on_change = lambda: on_main(self.changed)
        self.app.on_notice = lambda title, message: on_main(notify, title, message)
        self.app.on_offer = lambda offer, app: on_main(self.offer, offer, app)
        rumps.notifications(self.notification_clicked)
        pipeline.WORKER.on_event = self.on_job_event

        import server
        import window
        url, _token, self.httpd = server.serve(self.app)
        self.window = window.AppWindow(url)

        self.app.start()
        self.changed()
        rumps.events.before_quit.register(self.shutdown)

    # ------------------------------------------------------------ the menu

    def changed(self):
        """Something the menu shows changed. Main thread only."""
        self.icon = str(ICON if self.app.watching else ICON_PAUSED)
        self.template = True
        self.rebuild()

    def rebuild(self):
        """Draw the whole dropdown from menu.tree(). Main thread only."""
        self.status_items = []
        self.record_item = None
        items = []
        for item in menu.tree(self.app.facts()):
            drawn = self.draw(item)
            if item.role == "status":
                self.status_items.append(drawn)
            if item.action == "toggle_recording":
                self.record_item = drawn
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

    @rumps.timer(1)
    def tick(self, _):
        """Keep the status lines and the recording clock current. No network, no
        sqlite: cheap enough every second."""
        facts = self.app.status_facts()
        for line, item in zip(menu.status_lines(facts), self.status_items):
            item.title = line
        if self.record_item:                # the running clock while recording
            self.record_item.title = menu.record_item(facts["recording"], facts["call"]).title

    @rumps.timer(20)
    def probe_timer(self, _):
        threading.Thread(target=self.app.probe, daemon=True).start()

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
            notify("Notes ready", f"{title} — open it from the menu to read and send")
        elif kind == "error":
            notify("Could not process", f"{name}: {payload}")
        elif kind == "pull":
            notify("Model downloaded", name)
        self.rebuild()

    # ------------------------------------------------------------ a call noticed

    OFFERS = {"record": ("{app} is using the microphone", "Record the call?", "Record"),
              "stop": ("The {app} call seems over", "Stop recording?", "Stop")}

    def offer(self, offer, app):
        """A notification with a button. Main thread only."""
        title, message, button = self.OFFERS[offer]
        try:
            rumps.notification(title.format(app=app), "", message, data={"offer": offer},
                               action_button=button, other_button="Not now")
        except Exception:               # unbundled: no buttons, the window still offers
            notify(title.format(app=app), message)

    def notification_clicked(self, notification):
        """The button records or stops, if that is still what is on offer; a click
        anywhere else opens the window, whose banner says the same."""
        offer = (notification.data or {}).get("offer")
        if notification.activation_type != "action_button_clicked":
            self.window.show()
        elif offer and offer == self.app.call["offer"]:
            self.toggle_recording(None)

    # ------------------------------------------------------------ actions

    def toggle_recording(self, sender, _arg=None):
        try:
            self.app.set_recording(not self.app.recorder.recording)
        except RuntimeError as e:
            notify("Cannot record", str(e))

    def open_window(self, sender, _arg=None):
        self.window.show()

    def pick_file(self, sender, _arg=None):
        path = ask_for_file()
        if not path:
            return
        # Queued, not refused, if the worker is busy — and if this meeting has
        # been summarised before, its notes open instead of being made again.
        decision = self.app.submit(path)
        if decision.error:
            notify("Could not read that file", decision.error)
        elif decision.notes and not decision.queued:
            self.window.show(decision.meeting)

    def toggle_watching(self, sender, _arg=None):
        self.app.set_watching(not self.app.watching)

    def open_notes(self, sender, meeting):
        self.window.show(meeting)

    def reprocess(self, sender, meeting):
        decision = self.app.retry(meeting)
        if decision.error:
            notify("Cannot reprocess", decision.error)

    def reveal(self, sender, meeting):
        problem = self.app.reveal(meeting)
        if problem:
            notify("The transcript has moved", problem)

    def choose_folder(self, sender, _arg=None):
        path = ask_for_folder()
        if path:
            self.app.set_folder(path)

    def choose_model(self, sender, name):
        try:
            self.app.set_model(name)
        except ValueError as e:
            notify("Cannot change the model", str(e))

    def choose_mail(self, sender, mode):
        self.app.set_mail_mode(mode)

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
            problem = self.app.save_account(address, secret)
            if problem:
                status = "✕ " + problem
                continue
            notify("Mail account saved", f"Sending as {address.strip()}")
            return

    def edit_recipients(self, sender, _arg=None):
        answer = rumps.Window(
            "Everyone on this list gets every set of notes. One per line: a bare "
            'address, or "Name <address>".',
            "Default recipients",
            default_text="\n".join(mailer.split_addresses(paths.setting("RECIPIENTS"))),
            ok="Save", cancel="Cancel", dimensions=(300, 90)).run()
        if answer.clicked:
            self.app.set_recipients(answer.text.splitlines())

    def shutdown(self):
        """Before the app goes, from the menu, the Dock or ⌘Q alike."""
        self.app.stop()
        try:
            self.httpd.shutdown()
        except Exception:
            pass

    def quit(self, sender, _arg=None):
        rumps.quit_application()


class Delegate(rumps.rumps.NSApp):
    """rumps' own application delegate, plus a click on the Dock icon."""

    def applicationShouldHandleReopen_hasVisibleWindows_(self, _app, visible):
        if not visible:
            getattr(rumps.App, "*app_instance").window.show()
        return True


def run():
    from AppKit import NSApplication, NSApplicationActivationPolicyRegular

    import window

    main.init()
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    app.setMainMenu_(window.main_menu())
    rumps.rumps.NSApp = Delegate          # rumps builds its delegate from this name
    instance = TranscriptApp()
    AppHelper.callAfter(instance.window.show)
    instance.run()


if __name__ == "__main__":
    run()
