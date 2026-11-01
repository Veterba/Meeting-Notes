"""Noticing a call: Teams, Zoom or a browser starts using the microphone.

The Swift helper's watch mode (`mn-recorder --watch`) says which apps have the
microphone open, by bundle id, whenever that changes. `CallWatch` turns those
into the call apps it knows and reports which started and which stopped. It
never records anything: the controller offers, the user clicks.
"""
import json
import logging
import subprocess
import threading

import paths

log = logging.getLogger("pipeline")

# Bundle id prefix -> the name the offer shows. A browser using the microphone
# is most likely a Meet / Teams / Zoom call in a tab.
APPS = (
    ("com.microsoft.teams", "Teams"),
    ("us.zoom.", "Zoom"),
    ("com.google.Chrome", "Chrome"),
    ("company.thebrowser.", "Arc"),
    ("com.microsoft.edgemac", "Edge"),
    ("org.mozilla.firefox", "Firefox"),
    ("com.brave.Browser", "Brave"),
    ("com.apple.Safari", "Safari"),
    # Safari's media runs in WebKit's own process, not in Safari.
    ("com.apple.WebKit.GPU", "Safari"),
)


def call_apps(bundles) -> set:
    """The call apps among the bundle ids using the microphone."""
    return {name for bundle in bundles for prefix, name in APPS if bundle.startswith(prefix)}


class CallWatch:
    def __init__(self, on_change):
        # on_change(started, ended): two sets of app names, on the reader thread.
        self.on_change = on_change
        self.proc = None
        self.using = set()

    def start(self):
        helper = paths.recorder_binary()
        if not helper.exists():
            log.warning("Call detection is off: the recorder is not built (%s)", helper)
            return
        self.proc = subprocess.Popen([str(helper), "--watch"], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True, bufsize=1)
        threading.Thread(target=self._read, args=(self.proc,), name="calls",
                         daemon=True).start()

    def stop(self):
        proc, self.proc = self.proc, None
        if proc and proc.poll() is None:
            proc.stdin.close()              # the helper stops on end of input
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.terminate()

    def _read(self, proc):
        for line in proc.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("event") != "mic":
                continue
            now = call_apps(event.get("apps") or [])
            started, ended = now - self.using, self.using - now
            self.using = now
            if started or ended:
                try:
                    self.on_change(started, ended)
                except Exception as e:  # a broken listener must not stop the watch
                    log.error("Call listener failed: %s", e)
        if self.proc is proc:
            log.error("Call detection stopped: the watcher exited (%s)", proc.wait())
