"""One data folder: everything the app writes goes under it, and the first start
brings the old folder across exactly once.

MEETING_NOTES_DATA points at a temp folder for the whole suite (see conftest), so
none of this can reach the real ~/Library/Application Support/Meeting Notes.
"""
import logging
import re
import subprocess
import sys
from pathlib import Path

import llm
import mailer
import main
import parser
import paths
import pipeline

REAL_DATA = Path.home() / "Library" / "Application Support" / "Meeting Notes"


def fake_summarize(turns, meta, model=None, progress_cb=None):
    return {"title": meta["source"], "date": "2026-10-06", "participants": [],
            "summary": "s", "decisions": [], "action_items": [], "assumptions": [],
            "open_questions": []}


def test_the_tests_are_not_pointed_at_the_real_data_folder():
    assert paths.DATA != REAL_DATA
    assert REAL_DATA not in paths.DATA.parents


def test_everything_the_app_writes_is_under_the_data_folder(monkeypatch, transcript):
    for key in ("INBOX_DIR", "PROCESSED_DIR"):
        monkeypatch.delenv(key, raising=False)

    root = logging.getLogger()
    before = list(root.handlers)
    try:
        main.init()
    finally:
        for handler in set(root.handlers) - set(before):
            root.removeHandler(handler)
            handler.close()

    for folder in (paths.inbox(), paths.processed(), paths.logs()):
        assert folder.is_relative_to(paths.DATA), folder
    assert (paths.logs() / "pipeline.log").exists()      # the log opened there too
    assert paths.history_db().parent == paths.DATA

    monkeypatch.setattr(llm, "summarize", fake_summarize)
    src = transcript(paths.inbox(), "under-data.vtt")
    out = pipeline.process(pipeline.read(src))
    assert out.parent == paths.processed()
    assert out.is_relative_to(paths.DATA)


def test_importing_the_app_writes_nothing(tmp_path):
    """Only main.init touches the data folder, so an import cannot reach the real one."""
    data = tmp_path / "data"
    code = "import main, menu, history, llm, mailer, pipeline, watcher, server"
    subprocess.run([sys.executable, "-c", code], check=True, cwd=paths.CODE,
                   env={"MEETING_NOTES_DATA": str(data), "PATH": "/usr/bin:/bin"})
    assert not data.exists()


def test_env_example_documents_exactly_the_settings_the_app_reads():
    example = (paths.CODE / ".env.example").read_text(encoding="utf-8")
    documented = set(re.findall(r"^#?([A-Z_]+)=", example, re.M))
    assert documented == set(paths.DEFAULTS)


def test_a_threshold_in_env_reaches_the_transcript_check(temp_env, transcript, tmp_path):
    """The check used to read os.environ only, so .env could not change it."""
    parsed = parser.parse(transcript(tmp_path, "six-turns.vtt"))
    assert parser.looks_like_transcript(parsed)[0] is True

    temp_env.write_text("MIN_TURNS=50\n", encoding="utf-8")
    paths.reload()
    ok, reason = parser.looks_like_transcript(parsed)
    assert ok is False
    assert "at least 50" in reason


def test_a_relative_folder_setting_is_relative_to_the_data_folder(monkeypatch):
    """An app launched from Finder has / as its working directory."""
    monkeypatch.setenv("INBOX_DIR", "./inbox")
    assert paths.inbox() == paths.DATA / "inbox"


def test_the_environment_wins_over_the_settings_file(temp_env, monkeypatch):
    temp_env.write_text("MAIL_MODE=smtp\n", encoding="utf-8")
    paths.reload()
    assert paths.setting("MAIL_MODE") == "smtp"
    monkeypatch.setenv("MAIL_MODE", "outlook_draft")
    assert paths.setting("MAIL_MODE") == "outlook_draft"


def test_a_saved_setting_is_read_back_without_a_restart(temp_env, monkeypatch):
    monkeypatch.delenv("MAIL_MODE", raising=False)
    assert mailer.mail_mode() == "outlook_draft"         # the default
    paths.save_setting("MAIL_MODE", "smtp")
    assert paths.setting("MAIL_MODE") == "smtp"
    assert mailer.mail_mode() == "smtp"                  # no module copy to patch


def test_the_first_start_copies_the_old_folder_across_once(tmp_path):
    code, data = tmp_path / "code", tmp_path / "data"
    (code / "processed").mkdir(parents=True)
    (code / ".env").write_text("INBOX_DIR=/tmp/somewhere\n", encoding="utf-8")
    (code / "processed" / "old.json").write_text('{"title": "old"}', encoding="utf-8")

    copied = paths.migrate(code, data)

    assert len(copied) == 2, copied
    assert (data / ".env").read_text(encoding="utf-8") == "INBOX_DIR=/tmp/somewhere\n"
    assert (data / "processed" / "old.json").exists()
    # The originals are never removed.
    assert (code / ".env").exists()
    assert (code / "processed" / "old.json").exists()

    # Second start: nothing is copied and nothing is overwritten.
    (data / ".env").write_text("INBOX_DIR=/tmp/changed\n", encoding="utf-8")
    (data / "processed" / "old.json").write_text('{"title": "edited"}', encoding="utf-8")

    assert paths.migrate(code, data) == []
    assert "changed" in (data / ".env").read_text(encoding="utf-8")
    assert "edited" in (data / "processed" / "old.json").read_text(encoding="utf-8")

    # A meeting deleted on the data side stays deleted.
    (data / "processed" / "old.json").unlink()
    assert paths.migrate(code, data) == []
    assert not (data / "processed" / "old.json").exists()


def test_an_existing_env_is_never_replaced_but_old_notes_still_arrive(tmp_path):
    """The case on this machine: the bundle had already written its own .env."""
    code, data = tmp_path / "code", tmp_path / "data"
    (code / "processed").mkdir(parents=True)
    (code / ".env").write_text("MAIL_MODE=smtp\n", encoding="utf-8")
    (code / "processed" / "meeting.json").write_text("{}", encoding="utf-8")
    data.mkdir()
    (data / ".env").write_text("MAIL_MODE=outlook_draft\n", encoding="utf-8")

    copied = paths.migrate(code, data)

    assert (data / ".env").read_text(encoding="utf-8") == "MAIL_MODE=outlook_draft\n"
    assert (data / "processed" / "meeting.json").exists()
    assert any("meeting" in line or "file(s)" in line for line in copied), copied


def test_a_fresh_install_falls_back_to_env_example(tmp_path):
    code, data = tmp_path / "code", tmp_path / "data"
    code.mkdir()
    (code / ".env.example").write_text("INBOX_DIR=./inbox\nMAIL_MODE=outlook_draft\n",
                                       encoding="utf-8")

    paths.migrate(code, data)

    text = (data / ".env").read_text(encoding="utf-8")
    assert f"INBOX_DIR={Path.home() / 'Downloads'}" in text
    assert "INBOX_DIR=./inbox" not in text
