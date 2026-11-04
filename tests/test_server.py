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
    assert {"ollama_up", "ollama_installed", "model", "model_ready", "watching", "folder",
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


def test_a_noticed_call_is_offered_and_can_be_dismissed(api, app, temp_env, monkeypatch):
    monkeypatch.setattr(paths, "recorder_binary", lambda: temp_env.parent / "missing")
    app.calls.using = {"Zoom"}
    app._calls_changed({"Zoom"}, set())
    state = api("GET", "/api/state")[1]
    assert state["call"] == {"offer": "record", "app": "Zoom"} and state["call_detect"]
    assert api("POST", "/api/call/dismiss", {}) == (200, {"ok": True})
    assert api("GET", "/api/state")[1]["call"]["offer"] == ""
    api("POST", "/api/settings", {"call_detect": False})
    assert api("GET", "/api/state")[1]["call_detect"] is False


def test_watching_starts_and_stops_the_one_watcher(api, app, temp_env, box):
    api("POST", "/api/settings", {"watching": True})
    assert app.watching and app.watcher is not None
    api("POST", "/api/settings", {"watching": False})
    assert not app.watching and app.watcher is None


def test_the_model_is_downloaded_only_when_missing(api, app, worker, monkeypatch):
    monkeypatch.setattr(app, "probe", lambda: None)
    pulled = []
    monkeypatch.setattr(llm, "pull_model", lambda name, cb=None: pulled.append(name))
    monkeypatch.setattr(llm, "ollama_up", lambda: True)
    app.net.update(ollama_installed=True, model_ready=True)
    assert api("POST", "/api/model/download", {}) == (200, {"ok": True})
    app.net.update(model_ready=False)
    api("POST", "/api/model/download", {})
    assert wait_until(lambda: pulled == [llm.MODEL])


def test_no_model_is_downloaded_without_ollama_installed(api, app, worker, monkeypatch):
    pulled = []
    monkeypatch.setattr(llm, "pull_model", lambda name, cb=None: pulled.append(name))
    app.net.update(ollama_installed=False)

    status, r = api("POST", "/api/model/download", {})

    assert status == 400 and "Ollama" in r["error"]
    assert pulled == []


def test_files_are_picked_in_a_dialog_the_server_opens(api, app, box, worker, monkeypatch,
                                                       transcript):
    monkeypatch.setattr(llm, "summarize", fake_summarize)
    monkeypatch.setattr(llm, "ollama_up", lambda: True)
    monkeypatch.setattr(controller, "ask_for_files", lambda: [])
    assert api("POST", "/api/pick", {})[1] == {"ok": False, "cancelled": True}

    picked = [transcript(box.inbox, "picked.vtt"), box.inbox / "notes.txt"]
    picked[1].write_text("Just a line someone wrote down.")
    monkeypatch.setattr(controller, "ask_for_files", lambda: [str(p) for p in picked])
    status, r = api("POST", "/api/pick", {})

    # Picked by hand, so even a file that is no transcript goes to the model.
    assert status == 200 and [x["outcome"] for x in r["results"]] == ["queued", "queued"]
    assert wait_until(lambda: history.find(r["results"][0]["meeting"])["status"] == "ready")
    # The page learns what happened from the state, a drop on the window included.
    assert api("GET", "/api/state")[1]["imported"]["at"] == r["at"]

    status, r = api("POST", "/api/pick", {})
    assert r["results"][0]["outcome"] == "seen" and r["results"][0]["notes"]


def test_a_folder_of_transcripts_is_checked_file_by_file(api, box, worker, monkeypatch,
                                                          transcript, tmp_path):
    monkeypatch.setattr(llm, "summarize", fake_summarize)
    monkeypatch.setattr(llm, "ollama_up", lambda: True)
    folder = tmp_path / "old calls"
    (folder / "2025").mkdir(parents=True)
    transcript(folder, "a.vtt")
    transcript(folder / "2025", "b.vtt")
    transcript(folder, "same as a.vtt", topic="a")
    (folder / "invoice.docx").write_bytes(b"not a docx")
    (folder / "readme.md").write_text("# Read me\n\nInstall: run the script.\n")
    (folder / "photo.jpg").write_bytes(b"")
    monkeypatch.setattr(controller, "ask_for_folder", lambda prompt: str(folder))

    status, r = api("POST", "/api/pick/folder", {})

    got = {x["name"]: x["outcome"] for x in r["results"]}
    assert got == {"a.vtt": "queued", "same as a.vtt": "seen", "b.vtt": "queued",
                   "invoice.docx": "error", "readme.md": "skipped"}
    assert api("GET", "/api/state")[1]["skipped"] == 1


def test_files_turned_down_are_checked_again(api, box, worker, monkeypatch, transcript):
    monkeypatch.setattr(llm, "summarize", fake_summarize)
    monkeypatch.setattr(llm, "ollama_up", lambda: True)
    # As an older parser left them: one file that reads as a meeting now, one gone.
    history.record("old-hash", transcript(box.inbox, "now readable.vtt"), "skipped",
                   error="no speaker names found")
    history.record("gone-hash", box.inbox / "deleted.vtt", "skipped")
    assert api("GET", "/api/state")[1]["skipped"] == 2

    status, r = api("POST", "/api/recheck", {})

    assert [(x["name"], x["outcome"]) for x in r["results"]] == [("now readable.vtt", "queued")]
    assert history.find("old-hash") is None and history.find("gone-hash") is None
    assert wait_until(lambda: history.find(r["results"][0]["meeting"])["status"] == "ready")
    assert api("GET", "/api/state")[1]["skipped"] == 0


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


# ---------------------------------------------------------------- transcripts and recording

def test_a_meetings_transcript_is_read_again_from_its_source(api, meeting):
    status, r = api("GET", f"/api/meeting/{meeting}/transcript")
    assert status == 200 and r["error"] == "" and not r["recorded"]
    assert r["turns"] and set(r["turns"][0]) == {"speaker", "text"}
    assert api("GET", "/api/meeting/" + "0" * 64 + "/transcript")[1]["error"] == "no such meeting"


def test_the_live_lines_come_from_the_call_being_recorded(api, app, tmp_path):
    assert api("GET", "/api/live")[1]["lines"] == []

    from transcriber import Line, Live
    live = app.recorder.live = Live(tmp_path, transcribe=lambda s: "")
    live._add(Line("them", 1, 2, "hello"))
    live._add(Line("me", 3, 4, "hi"))

    status, r = api("GET", "/api/live")
    assert status == 200 and r["count"] == 2
    assert [line["text"] for line in r["lines"]] == ["hello", "hi"]
    # Nothing new: no lines sent again.
    assert api("GET", f"/api/live?version={r['version']}")[1]["lines"] is None


def test_deleting_and_restoring_through_the_window(api, meeting):
    from urllib.parse import quote
    assert api("POST", f"/api/meeting/{meeting}/archive", {}) == (200, {"ok": True})
    state = api("GET", "/api/state")[1]
    assert state["meetings"] == [] and len(state["archive"]) == 1
    entry = state["archive"][0]["id"]

    assert api("POST", f"/api/archive/{quote(entry)}/restore", {}) == (200, {"ok": True})
    assert [m["hash"] for m in api("GET", "/api/state")[1]["meetings"]] == [meeting]

    for bad in ("..%2F..%2Fetc", quote("2026-11-01 10-00-00 zzzzzz")):
        assert api("POST", f"/api/archive/{bad}/restore", {})[0] == 404
    assert api("POST", "/api/recording/..%2Fhistory.db/archive", {})[0] == 404
    # Only a recorded call has audio to transcribe again.
    assert api("POST", f"/api/meeting/{meeting}/transcribe", {})[1]["ok"] is False


def test_a_voice_is_named_in_the_notes_and_the_transcript(api, meeting):
    turns = api("GET", f"/api/meeting/{meeting}/transcript")[1]["turns"]
    old = turns[0]["speaker"]

    assert api("POST", f"/api/meeting/{meeting}/speaker", {"from": old, "to": "Anna"}
               ) == (200, {"ok": True})

    assert api("GET", f"/api/meeting/{meeting}/transcript")[1]["turns"][0]["speaker"] == "Anna"
    assert api("GET", f"/api/meeting/{meeting}")[1]["_speakers"] == {old: "Anna"}
    assert api("POST", f"/api/meeting/{meeting}/speaker", {"from": old, "to": " "}
               )[1]["ok"] is False
