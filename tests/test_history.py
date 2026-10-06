"""A meeting is remembered by what was said in it, and its file is never touched.

The fingerprint cases reuse the fixtures from test_parser, which is where the
shapes Teams hands out are described. tests/ is on sys.path, so importing that
module directly is how the same .vtt and .docx builders get used twice.
"""
import json
from pathlib import Path

import pytest

import history
import llm
import main
from helpers import fake_summarize, wait_until
from test_parser import A, B, DIALOGUE, REPORT, build_docx, build_vtt

OTHER = [(A, "Completely different meeting about the office move in November."),
         (B, "I booked the movers for the fourteenth and told the building manager."),
         (A, "Good, then we can let everyone know the desks change on the Friday."),
         (B, "I will put a plan on the wall and ask people to label their boxes.")]


def docx_lines(dialogue):
    return sum([[f"{s}\t0:{i * 7:02d}", t, ""] for i, (s, t) in enumerate(dialogue)], [])


@pytest.fixture
def model(monkeypatch):
    """No Ollama anywhere near these tests."""
    monkeypatch.setattr(llm, "summarize", fake_summarize)
    monkeypatch.setattr(llm, "ollama_up", lambda: True)


def finished(worker, meeting) -> bool:
    row = history.find(meeting)
    return bool(row) and row["status"] in ("ready", "failed")


# ---------------------------------------------------------------- fingerprint

def test_the_same_meeting_as_vtt_and_docx_is_one_fingerprint(tmp_path):
    as_vtt = build_vtt(tmp_path, "meeting", DIALOGUE, "classic")
    as_docx = build_docx(tmp_path, "meeting", docx_lines(DIALOGUE))

    assert as_vtt.read_bytes() != as_docx.read_bytes()
    import parser as tparser
    assert (history.fingerprint(tparser.parse(as_vtt))
            == history.fingerprint(tparser.parse(as_docx)))


def test_the_same_file_downloaded_twice_is_one_fingerprint(tmp_path):
    import parser as tparser
    first = build_docx(tmp_path, "Meeting", docx_lines(DIALOGUE))
    second = tmp_path / "Meeting (1).docx"
    second.write_bytes(first.read_bytes())

    assert (history.fingerprint(tparser.parse(first))
            == history.fingerprint(tparser.parse(second)))


def test_different_meetings_have_different_fingerprints(tmp_path):
    import parser as tparser
    one = build_vtt(tmp_path, "one", DIALOGUE, "classic")
    two = build_vtt(tmp_path, "two", OTHER + DIALOGUE[1:], "classic")

    assert (history.fingerprint(tparser.parse(one))
            != history.fingerprint(tparser.parse(two)))


# ---------------------------------------------------------------- the flow

def test_a_meeting_already_summarised_is_not_summarised_again(box, worker, model,
                                                              transcript):
    src = transcript(box.inbox, "sprint.vtt")

    first = main.submit(src)
    assert first["queued"] is True
    assert wait_until(lambda: finished(worker, first["hash"]))
    assert history.find(first["hash"])["status"] == "ready"

    again = main.submit(src)
    assert again["queued"] is False
    assert again["note"] == "already ready"
    assert again["notes"] == history.find(first["hash"])["notes"]
    assert len(list(box.processed.glob("*.json"))) == 1


def test_a_meeting_left_unfinished_is_queued_again(box, worker, model, transcript):
    """What a crash in the middle of a job leaves behind."""
    src = transcript(box.inbox, "half-done.vtt")
    turns = __import__("parser").parse(src)
    meeting = history.fingerprint(turns)
    history.record(meeting, src, "processing")      # as if the app died here

    decision = main.register(src)

    assert decision["queued"] is True
    assert decision["hash"] == meeting


def test_force_summarises_a_known_meeting_again(box, worker, model, transcript):
    src = transcript(box.inbox, "repeat.vtt")
    first = main.submit(src)
    assert wait_until(lambda: finished(worker, first["hash"]))
    notes = history.find(first["hash"])["notes"]

    again = main.submit(src, force=True)

    assert again["queued"] is True
    assert wait_until(lambda: history.find(first["hash"])["status"] == "ready")
    # The same notes file, rewritten — not a second one beside it.
    assert history.find(first["hash"])["notes"] == notes
    assert len(list(box.processed.glob("*.json"))) == 1


def test_the_source_file_is_never_touched(box, worker, model, transcript):
    src = transcript(box.inbox, "untouched.vtt")
    before, stat_before = src.read_bytes(), src.stat()

    decision = main.submit(src)
    assert wait_until(lambda: finished(worker, decision["hash"]))

    assert src.exists(), "the transcript was moved"
    assert src.read_bytes() == before
    assert src.stat().st_mtime == stat_before.st_mtime
    assert [p.name for p in box.inbox.iterdir()] == ["untouched.vtt"]
    # Only the notes went into processed/ — the transcript did not follow.
    assert [p.suffix for p in box.processed.iterdir()] == [".json"]


def test_a_file_that_is_not_a_transcript_is_recorded_as_skipped(box, worker, model):
    report = build_docx(box.inbox, "report", REPORT)   # test_parser's own fixture

    decision = main.submit_detected(report)

    assert decision["queued"] is False
    assert decision["status"] == "skipped"
    assert "nobody speaks twice" in decision["note"]
    assert history.find(decision["hash"])["status"] == "skipped"
    # Skipped files are not meetings and stay out of the list.
    assert history.listing() == []
    # Seen a second time, it is not reconsidered and not queued.
    assert main.submit_detected(report)["queued"] is False


def test_a_failure_is_recorded_and_can_be_retried(box, worker, monkeypatch, model,
                                                  transcript):
    src = transcript(box.inbox, "broken.vtt")
    boom = [True]

    def sometimes(turns, meta, model=None, progress_cb=None):
        if boom[0]:
            raise ValueError("model returned no valid JSON after 2 attempts")
        return fake_summarize(turns, meta, model, progress_cb)

    monkeypatch.setattr(llm, "summarize", sometimes)

    decision = main.submit(src)
    assert wait_until(lambda: finished(worker, decision["hash"]))
    row = history.find(decision["hash"])
    assert row["status"] == "failed"
    assert "no valid JSON" in row["error"]
    assert src.exists(), "a failed transcript must stay where it is"

    # A failed meeting is not retried by itself...
    assert main.submit(src)["queued"] is False
    # ...but can be, from its own row in the history.
    boom[0] = False
    assert main.retry(decision["hash"])["queued"] is True
    assert wait_until(lambda: history.find(decision["hash"])["status"] == "ready")
    assert history.find(decision["hash"])["error"] is None


def test_a_job_waits_for_ollama_and_finishes_when_it_comes_back(box, worker,
                                                                monkeypatch,
                                                                transcript):
    monkeypatch.setattr(llm, "summarize", fake_summarize)
    monkeypatch.setenv("OLLAMA_RETRY_SECONDS", "0.05")
    up = []
    monkeypatch.setattr(llm, "ollama_up", lambda: bool(up))
    src = transcript(box.inbox, "patient.vtt")

    decision = main.submit(src)

    assert wait_until(lambda: worker.snapshot()["stage"] == "waiting")
    row = history.find(decision["hash"])
    assert row["status"] == "found", "waiting for Ollama is not a failure"
    assert row["error"] is None

    up.append(True)                                   # Ollama comes back

    assert wait_until(lambda: history.find(decision["hash"])["status"] == "ready")
    assert json.loads(Path(history.find(decision["hash"])["notes"]).read_text(
        encoding="utf-8"))["title"] == "patient.vtt"


def test_sending_records_who_it_went_to(box, worker, model, transcript):
    src = transcript(box.inbox, "sent.vtt")
    decision = main.submit(src)
    assert wait_until(lambda: finished(worker, decision["hash"]))

    found = history.find_by_notes(history.find(decision["hash"])["notes"])
    assert found["hash"] == decision["hash"]
    history.mark_sent(found["hash"], ["team@example.com", "Alex <alex@example.com>"])

    row = history.find(decision["hash"])
    assert row["status"] == "sent"
    assert row["recipients"] == "team@example.com, Alex <alex@example.com>"
    assert row["sent_at"]
    assert history.listing()[0]["status"] == "sent"


# ---------------------------------------------------------------- the CLI

def test_the_cli_goes_through_the_history_and_force_overrides_it(box, model, capsys,
                                                                 monkeypatch,
                                                                 transcript):
    src = transcript(box.inbox, "cli.vtt")
    monkeypatch.setattr("sys.argv", ["main.py", "--once", str(src)])

    main.main()
    first = capsys.readouterr().out
    assert '"title": "cli.vtt"' in first
    meeting = history.find_by_notes(str(next(box.processed.glob("*.json"))))
    assert meeting["status"] == "ready"

    main.main()                                   # the same file again
    second = capsys.readouterr().out
    assert "already ready" in second
    assert "--force" in second

    monkeypatch.setattr("sys.argv", ["main.py", "--once", str(src), "--force"])
    main.main()
    assert '"title": "cli.vtt"' in capsys.readouterr().out
    assert len(list(box.processed.glob("*.json"))) == 1
