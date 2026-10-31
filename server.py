"""Local HTTP API behind the review window.

Bound to 127.0.0.1 and gated by a token generated at startup, so nothing else on
the machine can drive the pipeline through it. The window passes the token; the
page never leaves the loopback interface. A request whose Host is anything but
this server's own address is refused, so a web page cannot reach the API by
pointing a domain of its own at 127.0.0.1.

The window reviews and sends one meeting's notes, and that is all this serves.
A meeting is named by its hash; which file holds its notes is the history's
business, so no path ever comes in from the page. Everything else — the folder,
the model, the recipients, what to process and what to try again — lives in the
menu bar, which calls the pipeline directly.
"""
import json
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import history
import mailer
import paths
import pipeline

UI_DIR = paths.CODE / "ui"

TOKEN = secrets.token_urlsafe(24)

# /api/meeting/<hash> and /api/meeting/<hash>/send
RE_MEETING = re.compile(r"^/api/meeting/([0-9a-f]{64})(/send)?$")


def state() -> dict:
    return {
        "mail_mode": mailer.mail_mode(),
        "recipients": mailer.split_addresses(paths.setting("RECIPIENTS")),
        # Whatever the one worker is doing, whoever started it, so a meeting the
        # watcher picked up is visible while another one is being read.
        "job": pipeline.WORKER.snapshot(),
    }


def notes_file(meeting: str) -> Path:
    """Where a meeting's notes are, or None if it has none."""
    row = history.find(meeting)
    return Path(row["notes"]) if row and row["notes"] else None


def save(path: Path, data: dict):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass  # the pipeline log is the one that matters

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

    # -------------------------------------------------- routes

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html") and self._own_host():
            html = (UI_DIR / "index.html").read_text(encoding="utf-8")
            return self._send(200, html.replace("__TOKEN__", TOKEN).encode("utf-8"),
                              "text/html; charset=utf-8")
        if not self._authorised():
            return self._json({"error": "forbidden"}, 403)

        if u.path == "/api/state":
            return self._json(state())
        m = RE_MEETING.match(u.path)
        if m and not m.group(2):
            path = notes_file(m.group(1))
            if not (path and path.exists()):
                return self._json({"error": "this meeting has no notes"}, 404)
            return self._json(json.loads(path.read_text(encoding="utf-8")))
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._authorised():
            return self._json({"error": "forbidden"}, 403)
        m = RE_MEETING.match(urlparse(self.path).path)
        if not m:
            return self._json({"error": "not found"}, 404)
        try:
            body = self._body()
        except Exception as e:
            return self._json({"error": f"bad request: {e}"}, 400)
        meeting, sending = m.group(1), bool(m.group(2))
        path = notes_file(meeting)
        if not path:
            return self._json({"error": "this meeting has no notes"}, 404)

        data = body["data"]
        if sending:
            data["recipients"] = body["recipients"]
        save(path, data)                    # what was sent is what is kept
        if not sending:
            return self._json({"ok": True})
        try:
            message = mailer.deliver(data, body["recipients"])
        except Exception as e:
            return self._json({"ok": False, "error": str(e)})
        history.mark_sent(meeting, body["recipients"])
        return self._json({"ok": True, "message": message})


def serve() -> tuple:
    """Start the API on a free loopback port. Returns (url, token, httpd)."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    return f"http://127.0.0.1:{port}", TOKEN, httpd
