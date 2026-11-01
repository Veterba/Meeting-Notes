"""Local HTTP API behind the app window.

Bound to 127.0.0.1 and gated by a token generated at startup, so nothing else on
the machine can drive the app through it. The window passes the token; the page
never leaves the loopback interface. A request whose Host is anything but this
server's own address is refused, so a web page cannot reach the API by pointing a
domain of its own at 127.0.0.1.

A meeting is named by its hash; which file holds its notes is the history's
business, so no path ever comes in from the page. A file or a folder is chosen
in a native dialog the server opens itself. Everything the window can change goes
through the same `Controller` the menu bar uses.
"""
import json
import mimetypes
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import archive
import controller
import history
import mailer
import parser as tparser
import paths
import speakers

# The React build (ui/ is its source; npm run build writes ui/dist).
UI_DIR = paths.CODE / "ui" / "dist"

TOKEN = secrets.token_urlsafe(24)

# /api/meeting/<hash>, and what can be done to it
RE_MEETING = re.compile(r"^/api/meeting/([0-9a-f]{64})"
                        r"(?:/(send|retry|reveal|transcript|archive|transcribe|speaker))?$")
# /api/recording/<folder name>/... and /api/archive/<entry id>/restore, names URL-encoded
RE_RECORDING = re.compile(r"^/api/recording/([^/]+)/(archive|transcribe)$")
RE_RESTORE = re.compile(r"^/api/archive/([^/]+)/restore$")


def state(app: controller.Controller) -> dict:
    """Everything the window shows, in one poll."""
    facts = app.facts(meetings=200)
    recordings = paths.recordings()
    for m in facts["meetings"]:
        m["recorded"] = Path(m["source"]).is_relative_to(recordings)
    return {
        **{k: facts[k] for k in ("ollama_up", "model", "model_ready",
                                 "watching", "mail_mode", "meetings")},
        "folder": str(facts["folder"]),
        # Whatever the one worker is doing, whoever started it.
        "job": facts["job"],
        "recipients": mailer.split_addresses(paths.setting("RECIPIENTS")),
        "account": mailer.account(),
        "recording": facts["recording"],
        "call": facts["call"],
        "call_detect": facts["call_detect"],
        "recordings": app.lone_recordings(),
        "archive": archive.entries(),
        "archive_days": archive.days(),
    }


def notes_file(meeting: str) -> Path:
    """Where a meeting's notes are, or None if it has none."""
    row = history.find(meeting)
    return Path(row["notes"]) if row and row["notes"] else None


def transcript(meeting: str) -> dict:
    """What was said in a meeting, read again from its source: the downloaded
    file, or the recording's own transcript."""
    row = history.find(meeting)
    if not row:
        return {"error": "no such meeting", "turns": []}
    src = Path(row["source"])
    try:
        turns = tparser.parse(src).turns
    except Exception as e:
        return {"error": f"the transcript cannot be read: {e}" if src.exists()
                else f"the transcript is no longer at {src}", "turns": []}
    notes = notes_file(meeting)
    data = json.loads(notes.read_text(encoding="utf-8")) if notes and notes.exists() else {}
    return {"error": "", "source": src.name,
            "recorded": src.is_relative_to(paths.recordings()),
            "turns": [{"speaker": s, "text": t} for s, t in speakers.named(turns, data)]}


def decision(d) -> dict:
    return {"ok": not d.error, "queued": d.queued, "meeting": d.meeting,
            "status": d.status, "note": d.note, "error": d.error}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass  # the pipeline log is the one that matters

    @property
    def app(self) -> controller.Controller:
        return self.server.app

    # -------------------------------------------------- plumbing

    def _send(self, code, body: bytes, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _own_host(self) -> bool:
        host, port = self.server.server_address[:2]
        return self.headers.get("Host") == f"{host}:{port}"

    def _authorised(self) -> bool:
        return self._own_host() and self.headers.get("X-Token") == TOKEN

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _static(self, path: str):
        """The page, with the token in it, or one of the build's own assets."""
        if path in ("/", "/index.html"):
            html = (UI_DIR / "index.html").read_text(encoding="utf-8")
            return self._send(200, html.replace("__TOKEN__", TOKEN).encode("utf-8"),
                              "text/html; charset=utf-8")
        assets = (UI_DIR / "assets").resolve()
        file = (UI_DIR / path.lstrip("/")).resolve()
        if file.parent != assets or not file.is_file():
            return self._json({"error": "not found"}, 404)
        ctype = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        return self._send(200, file.read_bytes(), ctype)

    # -------------------------------------------------- routes

    def do_GET(self):
        path = urlparse(self.path).path
        if not self._own_host():
            return self._json({"error": "forbidden"}, 403)
        if not path.startswith("/api/"):
            return self._static(path)
        if not self._authorised():
            return self._json({"error": "forbidden"}, 403)

        if path == "/api/state":
            return self._json(state(self.app))
        if path == "/api/live":
            return self._json(self._live(parse_qs(urlparse(self.path).query)))
        m = RE_MEETING.match(path)
        if m and m.group(2) == "transcript":
            return self._json(transcript(m.group(1)))
        if m and not m.group(2):
            notes = notes_file(m.group(1))
            if not (notes and notes.exists()):
                return self._json({"error": "this meeting has no notes"}, 404)
            return self._json(json.loads(notes.read_text(encoding="utf-8")))
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._authorised():
            return self._json({"error": "forbidden"}, 403)
        path = urlparse(self.path).path
        try:
            body = self._body()
        except Exception as e:
            return self._json({"error": f"bad request: {e}"}, 400)

        m = RE_MEETING.match(path)
        if m:
            return self._meeting(m.group(1), m.group(2), body)
        m = RE_RECORDING.match(path) or RE_RESTORE.match(path)
        if m:
            return self._library(m, unquote(m.group(1)))
        route = {"/api/pick": self._pick, "/api/folder": self._folder,
                 "/api/settings": self._settings, "/api/account": self._account,
                 "/api/record": self._record,
                 "/api/call/dismiss": self._dismiss_call,
                 "/api/model/download": self._download_model,
                 "/api/account/test": self._account_test}.get(path)
        if not route:
            return self._json({"error": "not found"}, 404)
        try:
            return self._json(route(body))
        except (KeyError, ValueError) as e:
            return self._json({"error": f"bad request: {e}"}, 400)
        except RuntimeError as e:
            return self._json({"ok": False, "error": str(e)})

    def _live(self, query: dict) -> dict:
        """The lines of the call being recorded (or the last one), unless the
        page already has this `version` of them."""
        live = self.app.recorder.live
        if not live:
            return {"status": "", "error": "", "count": 0, "version": 0, "lines": []}
        return live.snapshot(int((query.get("version") or ["-1"])[0]))

    def _library(self, m, name: str):
        """Deleting, restoring and transcribing again: by a recording's folder
        name or an archive entry's id, each checked against what exists."""
        action = m.group(2) if m.re is RE_RECORDING else "restore"
        work = {"archive": self.app.archive_recording, "transcribe": self.app.retranscribe,
                "restore": self.app.restore}[action]
        try:
            work(name)
        except KeyError as e:
            return self._json({"ok": False, "error": str(e).strip("'")}, 404)
        except (RuntimeError, OSError) as e:
            return self._json({"ok": False, "error": str(e)})
        return self._json({"ok": True})

    def _meeting(self, meeting: str, action: str, body: dict):
        if action in ("archive", "transcribe"):
            try:
                if action == "archive":
                    self.app.archive_meeting(meeting)
                else:
                    row = history.find(meeting)
                    folder = row and archive.recording_of(row["source"])
                    if not folder:
                        return self._json({"ok": False, "error": "only a recorded call "
                                           "can be transcribed again"})
                    self.app.retranscribe(folder.name)
            except KeyError as e:
                return self._json({"ok": False, "error": str(e).strip("'")}, 404)
            except (RuntimeError, OSError) as e:
                return self._json({"ok": False, "error": str(e)})
            return self._json({"ok": True})
        if action == "retry":
            return self._json(decision(self.app.retry(meeting)))
        if action == "reveal":
            error = self.app.reveal(meeting)
            return self._json({"ok": not error, "error": error})

        notes = notes_file(meeting)
        if not notes:
            return self._json({"error": "this meeting has no notes"}, 404)
        if action == "speaker":
            # A name for a voice: everywhere in the notes, and in the transcript.
            data = json.loads(notes.read_text(encoding="utf-8"))
            try:
                data = speakers.rename(data, body["from"], body["to"])
            except ValueError as e:
                return self._json({"ok": False, "error": str(e)})
            notes.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            return self._json({"ok": True})
        data = body["data"]
        if action == "send":
            data["recipients"] = body["recipients"]
        notes.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                         encoding="utf-8")      # what was sent is what is kept
        if action != "send":
            return self._json({"ok": True})
        try:
            message = mailer.deliver(data, body["recipients"])
        except Exception as e:
            return self._json({"ok": False, "error": str(e)})
        history.mark_sent(meeting, body["recipients"])
        return self._json({"ok": True, "message": message})

    # Each of these returns what the page is told.

    def _pick(self, _body):
        path = controller.ask_for_file()
        if not path:
            return {"ok": False, "cancelled": True}
        return decision(self.app.submit(path))

    def _folder(self, _body):
        path = controller.ask_for_folder()
        if not path:
            return {"ok": False, "cancelled": True}
        self.app.set_folder(path)
        return {"ok": True}

    def _settings(self, body):
        """One or more of: watching, mail_mode, recipients, archive_days,
        call_detect."""
        setters = {"watching": lambda v: self.app.set_watching(bool(v)),
                   "mail_mode": self.app.set_mail_mode,
                   "recipients": self.app.set_recipients,
                   "archive_days": self.app.set_archive_days,
                   "call_detect": lambda v: self.app.set_call_detect(bool(v))}
        unknown = set(body) - set(setters)
        if unknown:
            raise KeyError(", ".join(sorted(unknown)))
        for key, value in body.items():
            setters[key](value)
        return {"ok": True}

    def _record(self, body):
        self.app.set_recording(bool(body["on"]))
        return {"ok": True}

    def _download_model(self, _body):
        self.app.download_model()
        return {"ok": True}

    def _dismiss_call(self, _body):
        self.app.dismiss_call()
        return {"ok": True}

    def _account(self, body):
        problem = self.app.save_account(body["address"], body.get("password", ""))
        return {"ok": not problem, "error": problem}

    def _account_test(self, body):
        # Sends one real message, from the account to itself — only ever from the
        # button in the window.
        ok, message = mailer.send_test(body["address"], body.get("password") or None)
        return {"ok": ok, "message": message}


def serve(app: controller.Controller) -> tuple:
    """Start the API on a free loopback port. Returns (url, token, httpd)."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    httpd.app = app
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    return f"http://127.0.0.1:{port}", TOKEN, httpd
