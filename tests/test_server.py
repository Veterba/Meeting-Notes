"""The window's API: meetings by their hash, never by a path; settings through
the same controller the menu bar uses.

The server runs for real on a loopback port. Delivery, the dialogs and Finder are
replaced, so nothing is ever sent or opened. The page is a stand-in for the React
build, so the tests need no npm.
"""
import http.client
import json

import pytest

import controller
import history
import llm
import mailer
import paths
import pipeline
import server
from helpers import fake_summarize, wait_until


@pytest.fixture
def app(monkeypatch, tmp_path):
    ui = tmp_path / "ui-dist"
    (ui / "assets").mkdir(parents=True)
    (ui / "index.html").write_text('<script>window.TOKEN = "__TOKEN__";</script>')
    (ui / "assets" / "index.js").write_text("console.log('page')")
    monkeypatch.setattr(server, "UI_DIR", ui)
    a = controller.Controller()
    a.changes = []
    a.on_change = lambda: a.changes.append(1)
    yield a
    a.stop()


@pytest.fixture
def api(app):
    url, token, httpd = server.serve(app)
    host, port = httpd.server_address[:2]

    def call(method, path, body=None, token=token, host_header=f"{host}:{port}"):
        conn = http.client.HTTPConnection(host, port, timeout=5)
        headers = {"Host": host_header, "X-Token": token,
                   "Content-Type": "application/json"}
        conn.request(method, path, json.dumps(body) if body is not None else None, headers)
        r = conn.getresponse()
        data = r.read()
        conn.close()
        return r.status, (json.loads(data) if "json" in r.getheader("Content-Type")
                          else data.decode("utf-8"))

    yield call
    httpd.shutdown()


@pytest.fixture
def meeting(box, worker, monkeypatch, transcript):
    monkeypatch.setattr(llm, "summarize", fake_summarize)
    monkeypatch.setattr(llm, "ollama_up", lambda: True)
    decision = pipeline.submit(transcript(box.inbox, "review.vtt"))
    assert wait_until(lambda: history.find(decision.meeting)["status"] == "ready")
    return decision.meeting


def test_the_notes_are_read_and_saved_by_the_meetings_hash(api, meeting):
    status, notes = api("GET", f"/api/meeting/{meeting}")
    assert status == 200
    assert notes["title"] == "review.vtt"

    notes["title"] = "Edited in the window"
    assert api("POST", f"/api/meeting/{meeting}", {"data": notes}) == (200, {"ok": True})

    assert api("GET", f"/api/meeting/{meeting}")[1]["title"] == "Edited in the window"


def test_sending_keeps_what_was_sent_and_marks_the_meeting(api, meeting, monkeypatch):
    delivered = []
    monkeypatch.setattr(mailer, "deliver",
                        lambda data, recipients: delivered.append((data, recipients)) or "ok")
    notes = api("GET", f"/api/meeting/{meeting}")[1]

    status, r = api("POST", f"/api/meeting/{meeting}/send",
                    {"data": notes, "recipients": ["team@example.com"]})

    assert (status, r["ok"]) == (200, True)
    assert delivered[0][1] == ["team@example.com"]
    assert history.find(meeting)["status"] == "sent"
    assert api("GET", f"/api/meeting/{meeting}")[1]["recipients"] == ["team@example.com"]


def test_a_path_is_never_accepted_from_the_page(api, meeting, tmp_path):
    status, _ = api("GET", f"/api/notes?path={tmp_path / 'anything.json'}")
    assert status == 404
    status, _ = api("POST", "/api/notes", {"path": str(tmp_path / "x.json"), "data": {}})
    assert status == 404
    assert not (tmp_path / "x.json").exists()
    assert api("GET", "/api/meeting/" + "0" * 64)[0] == 404


def test_without_the_token_or_from_another_host_nothing_is_served(api, meeting):
    assert api("GET", f"/api/meeting/{meeting}", token="wrong")[0] == 403
    # A domain pointed at 127.0.0.1 by a web page: not even the page with the token.
    assert api("GET", "/", host_header="evil.example:80")[0] == 403
    assert api("GET", f"/api/meeting/{meeting}", host_header="evil.example:80")[0] == 403
    status, page = api("GET", "/")
    assert status == 200 and server.TOKEN in page
    assert api("GET", "/assets/index.js", host_header="evil.example:80")[0] == 403


def test_the_page_and_its_assets_are_served_and_nothing_else(api):
    status, body = api("GET", "/assets/index.js")
    assert (status, body) == (200, "console.log('page')")
    for path in ("/assets/../index.html", "/assets/%2e%2e/../server.py", "/server.py",
                 "/assets/missing.js"):
        assert api("GET", path)[0] == 404, path


# ---------------------------------------------------------------- the rest of the window

def test_the_state_has_what_the_window_shows(api, meeting):
    status, state = api("GET", "/api/state")
    assert status == 200
    assert {"ollama_up", "model", "models", "installed", "watching", "folder",
            "mail_mode", "recipients", "account", "job", "meetings"} <= set(state)
    assert [m["hash"] for m in state["meetings"]] == [meeting]


def test_settings_change_through_the_controller(api, app, temp_env):
    assert api("POST", "/api/settings", {"mail_mode": "smtp",
                                         "recipients": [" a@x.com ", "", "B <b@x.com>"]}
               ) == (200, {"ok": True})
    assert paths.setting("MAIL_MODE") == "smtp"
    assert paths.setting("RECIPIENTS") == "a@x.com, B <b@x.com>"
    assert app.changes

    assert api("POST", "/api/settings", {"mail_mode": "pigeon"})[0] == 400
    assert api("POST", "/api/settings", {"OLLAMA_URL": "http://evil"})[0] == 400
    assert paths.setting("MAIL_MODE") == "smtp"


def test_watching_starts_and_stops_the_one_watcher(api, app, temp_env, box):
    api("POST", "/api/settings", {"watching": True})
    assert app.watching and app.watcher is not None
    api("POST", "/api/settings", {"watching": False})
    assert not app.watching and app.watcher is None


def test_choosing_a_model_downloads_only_a_missing_one(api, app, temp_env, worker,
                                                        monkeypatch):
    monkeypatch.setattr(app, "probe", lambda: None)
    pulled = []
    monkeypatch.setattr(llm, "pull_model", lambda name, cb=None: pulled.append(name))
    app.net.update(ollama_up=True, installed=["qwen3.5:9b"])

    api("POST", "/api/settings", {"model": "qwen3.5:9b"})
    api("POST", "/api/settings", {"model": "qwen3:14b"})

    assert paths.setting("OLLAMA_MODEL") == "qwen3:14b"
    assert wait_until(lambda: pulled == ["qwen3:14b"])


def test_no_model_is_chosen_or_downloaded_while_ollama_is_down(api, app, temp_env, worker,
                                                                 monkeypatch):
    pulled = []
    monkeypatch.setattr(llm, "pull_model", lambda name, cb=None: pulled.append(name))
    app.net.update(ollama_up=False)
    before = paths.setting("OLLAMA_MODEL")

    status, r = api("POST", "/api/settings", {"model": "qwen3:14b"})

    assert status == 400 and "Ollama" in r["error"]
    assert paths.setting("OLLAMA_MODEL") == before and pulled == []


def test_a_file_is_picked_in_a_dialog_the_server_opens(api, box, worker, monkeypatch,
                                                       transcript):
    monkeypatch.setattr(llm, "summarize", fake_summarize)
    monkeypatch.setattr(llm, "ollama_up", lambda: True)
    monkeypatch.setattr(controller, "ask_for_file", lambda: "")
    assert api("POST", "/api/pick", {})[1] == {"ok": False, "cancelled": True}

    path = transcript(box.inbox, "picked.vtt")
    monkeypatch.setattr(controller, "ask_for_file", lambda: str(path))
    status, r = api("POST", "/api/pick", {})

    assert (status, r["ok"], r["queued"]) == (200, True, True)
    assert wait_until(lambda: history.find(r["meeting"])["status"] == "ready")


def test_a_folder_is_chosen_in_a_dialog_too(api, app, temp_env, tmp_path, monkeypatch):
    monkeypatch.setattr(controller, "ask_for_folder", lambda: str(tmp_path) + "/")
    assert api("POST", "/api/folder", {}) == (200, {"ok": True})
    assert paths.setting("INBOX_DIR") == str(tmp_path)


def test_a_meeting_is_reprocessed_and_revealed_by_its_hash(api, meeting, monkeypatch):
    opened = []
    monkeypatch.setattr(controller.subprocess, "run", lambda cmd, **kw: opened.append(cmd))
    assert api("POST", f"/api/meeting/{meeting}/reveal", {}) == (200, {"ok": True, "error": ""})
    assert opened[0][:2] == ["open", "-R"]

    status, r = api("POST", f"/api/meeting/{meeting}/retry", {})
    assert (status, r["ok"], r["queued"]) == (200, True, True)


def test_the_mail_account_is_saved_and_tested_without_a_password_coming_back(
        api, monkeypatch):
    saved, tested = [], []
    monkeypatch.setattr(mailer, "save_account", lambda a, s: saved.append((a, s)) or "")
    monkeypatch.setattr(mailer, "send_test",
                        lambda a, s=None: tested.append((a, s)) or (True, "Sent."))

    assert api("POST", "/api/account", {"address": "me@gmail.com", "password": "pw"}
               ) == (200, {"ok": True, "error": ""})
    assert api("POST", "/api/account/test", {"address": "me@gmail.com"}
               ) == (200, {"ok": True, "message": "Sent."})
    assert saved == [("me@gmail.com", "pw")] and tested == [("me@gmail.com", None)]
    assert "pw" not in json.dumps(api("GET", "/api/state")[1])
