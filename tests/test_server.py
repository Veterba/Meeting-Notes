"""The review window's API: one meeting at a time, by its hash, never by a path.

The server runs for real on a loopback port; only the delivery is replaced, so
nothing is ever sent.
"""
import http.client
import json

import pytest

import history
import llm
import mailer
import pipeline
import server
from helpers import fake_summarize, wait_until


@pytest.fixture
def api():
    url, token, httpd = server.serve()
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
