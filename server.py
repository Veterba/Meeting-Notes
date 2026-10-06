"""Local HTTP API behind the review window.

Bound to 127.0.0.1 and gated by a token generated at startup, so nothing else on
the machine can drive the pipeline through it. The window passes the token; the
page never leaves the loopback interface.

The window reviews and sends one meeting's notes, and that is all this serves.
Everything else — the folder, the model, the recipients, what to process and what
to try again — lives in the menu bar, which calls the pipeline directly.
"""
import json
import mimetypes
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import history
import mailer
import main as pipeline
import paths

UI_DIR = paths.CODE / "ui"

TOKEN = secrets.token_urlsafe(24)


def state() -> dict:
    return {
        "mail_mode": mailer.mail_mode(),
        "recipients": mailer.split_addresses(paths.setting("RECIPIENTS")),
        # Whatever the one worker is doing, whoever started it, so a meeting the
        # watcher picked up is visible while another one is being read.
        "job": pipeline.WORKER.snapshot(),
    }


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

    def _authorised(self, query) -> bool:
        return (self.headers.get("X-Token") == TOKEN
                or query.get("t", [None])[0] == TOKEN)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    # -------------------------------------------------- routes

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)

        if u.path in ("/", "/index.html"):
            html = (UI_DIR / "index.html").read_text(encoding="utf-8")
            return self._send(200, html.replace("__TOKEN__", TOKEN).encode("utf-8"),
                              "text/html; charset=utf-8")

        asset = (UI_DIR / u.path.lstrip("/")).resolve()
        if asset.is_file() and UI_DIR.resolve() in asset.parents:
            ctype = mimetypes.guess_type(asset.name)[0] or "application/octet-stream"
            return self._send(200, asset.read_bytes(), ctype)

        if not self._authorised(q):
            return self._json({"error": "forbidden"}, 403)

        if u.path == "/api/state":
            return self._json(state())
        if u.path == "/api/notes":
            p = Path(q["path"][0])
            return self._json(json.loads(p.read_text(encoding="utf-8")))
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        if not self._authorised(parse_qs(u.query)):
            return self._json({"error": "forbidden"}, 403)
        try:
            body = self._body()
        except Exception as e:
            return self._json({"error": f"bad request: {e}"}, 400)

        if u.path == "/api/notes":
            p = Path(body["path"])
            p.write_text(json.dumps(body["data"], ensure_ascii=False, indent=2),
                         encoding="utf-8")
            return self._json({"ok": True})

        if u.path == "/api/send":
            try:
                message = mailer.deliver(body["data"], body["recipients"])
            except Exception as e:
                return self._json({"ok": False, "error": str(e)})
            meeting = history.find_by_notes(body.get("path", ""))
            if meeting:
                history.mark_sent(meeting["hash"], body["recipients"])
            return self._json({"ok": True, "message": message})

        return self._json({"error": "not found"}, 404)


def serve() -> tuple:
    """Start the API on a free loopback port. Returns (url, token, httpd)."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    return f"http://127.0.0.1:{port}", TOKEN, httpd
