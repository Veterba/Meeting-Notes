"""Local HTTP API behind the app window.

Bound to 127.0.0.1 and gated by a token generated at startup, so nothing else on
the machine can drive the pipeline through it. The window passes the token; the
page never leaves the loopback interface.
"""
import json
import mimetypes
import os
import secrets
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import llm
import mailer
import main as pipeline

from paths import CODE

UI_DIR = CODE / "ui"

TOKEN = secrets.token_urlsafe(24)

# Set by the menu bar app so settings changes restart the watcher.
on_settings_changed = None


def _env_flag(key: str, default: bool = True) -> bool:
    return os.getenv(key, "1" if default else "0").strip().lower() \
        not in ("0", "false", "no", "off", "")


def state() -> dict:
    inbox = pipeline.INBOX_DIR
    files = []
    # Same age rule as the watcher, so the list shows what would actually be
    # picked up — not every .docx that has ever been downloaded.
    cutoff = time.time() - pipeline.SCAN_MAX_AGE_HOURS * 3600
    if inbox.exists():
        for f in sorted(inbox.iterdir(), key=lambda f: f.stat().st_mtime, reverse=True):
            if (f.is_file() and f.suffix.lower() in pipeline.EXTENSIONS
                    and f.stat().st_mtime >= cutoff):
                files.append({"path": str(f), "name": f.name,
                              "modified": datetime.fromtimestamp(f.stat().st_mtime)
                              .strftime("%d %b %H:%M")})
    history = []
    for f in sorted(pipeline.PROCESSED_DIR.glob("*.json"),
                    key=lambda f: f.stat().st_mtime, reverse=True)[:25]:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            title, date = d.get("title") or f.stem, d.get("date", "")
        except Exception:
            title, date = f.name, ""
        history.append({"path": str(f), "title": title, "date": date,
                        "modified": datetime.fromtimestamp(f.stat().st_mtime)
                        .strftime("%d %b %H:%M")})

    up = llm.ollama_up()
    model = llm.default_model()
    installed = llm.installed_models() if up else []
    choices = []
    for m in [llm.MODEL_SMALL, llm.MODEL_MID, llm.MODEL_BIG] + installed:
        if m not in choices:
            choices.append(m)
    return {
        "ollama": up,
        "ollama_installed": bool(llm.ollama_binary()),
        "ram": round(llm.ram_gb()),
        "model": model,
        "model_ready": up and llm.has_model(model),
        "models": choices,
        "folder": str(inbox),
        "watching": _env_flag("WATCH_ENABLED"),
        "mail_mode": mailer.MAIL_MODE,
        "recipients": mailer.split_addresses(os.getenv("RECIPIENTS", "")),
        "files": files[:25],
        "history": history,
        "num_ctx": llm.NUM_CTX,
        # Whatever the one worker is doing, whoever started it — so the window
        # shows a file the watcher picked up as well as one chosen here.
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
        if u.path == "/api/job":
            return self._json(pipeline.WORKER.snapshot())
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

        if u.path == "/api/process":
            # Chosen by hand, so the transcript check is skipped on purpose and
            # the file stays where it is. Queued if the worker is busy.
            return self._json(pipeline.WORKER.submit(body["path"]))

        if u.path == "/api/ollama":
            return self._json({"ok": llm.start_ollama(), "state": state()})

        if u.path == "/api/pull":
            return self._json(pipeline.WORKER.submit(body["model"], kind="pull"))

        if u.path == "/api/notes":
            p = Path(body["path"])
            p.write_text(json.dumps(body["data"], ensure_ascii=False, indent=2),
                         encoding="utf-8")
            return self._json({"ok": True})

        if u.path == "/api/send":
            try:
                msg = mailer.deliver(body["data"], body["recipients"])
                return self._json({"ok": True, "message": msg})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)})

        if u.path == "/api/settings":
            from menubar import save_setting
            for key, value in body.items():
                save_setting(key, str(value))
            if "MAIL_MODE" in body:
                mailer.MAIL_MODE = str(body["MAIL_MODE"])
            if on_settings_changed:
                on_settings_changed(body)
            return self._json({"ok": True, "state": state()})

        if u.path == "/api/choose":
            from menubar import osa
            kind = body.get("kind", "folder")
            script = ('POSIX path of (choose folder with prompt "Where transcripts arrive")'
                      if kind == "folder" else
                      'POSIX path of (choose file with prompt "Choose a transcript" '
                      'of type {"vtt", "docx"})')
            return self._json({"path": osa(script)})

        return self._json({"error": "not found"}, 404)


def serve() -> tuple:
    """Start the API on a free loopback port. Returns (url, token, httpd)."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    return f"http://127.0.0.1:{port}", TOKEN, httpd
