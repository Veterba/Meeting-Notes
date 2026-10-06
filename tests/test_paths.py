"""One data folder: everything the app writes goes under it, and the first start
brings the old folder across exactly once.

MEETING_NOTES_DATA points at a temp folder for the whole suite (see conftest), so
none of this can reach the real ~/Library/Application Support/Meeting Notes.
"""
from pathlib import Path

import pytest

import llm
import mailer
import main
import paths

REAL_DATA = Path.home() / "Library" / "Application Support" / "Meeting Notes"


def fake_summarize(turns, meta, model=None, progress_cb=None):
    return {"title": meta["source"], "date": "2026-10-06", "participants": [],
            "summary": "s", "decisions": [], "action_items": [], "assumptions": [],
            "open_questions": []}


@pytest.fixture
def temp_env(tmp_path, monkeypatch):
    """Point the settings file somewhere disposable, and leave no trace."""
    monkeypatch.setattr(paths, "DATA", tmp_path)
    monkeypatch.setattr(paths, "ENV_FILE", tmp_path / ".env")
    paths.reload()
    yield tmp_path / ".env"
    monkeypatch.undo()
    paths.reload()


def test_the_tests_are_not_pointed_at_the_real_data_folder():
    assert paths.DATA != REAL_DATA
    assert REAL_DATA not in paths.DATA.parents


def test_everything_the_app_writes_is_under_the_data_folder(monkeypatch, transcript):
    for key in ("INBOX_DIR", "PROCESSED_DIR"):
        monkeypatch.delenv(key, raising=False)

    for folder in (paths.inbox(), paths.processed(), paths.logs()):
        assert folder.is_relative_to(paths.DATA), folder
    assert (paths.logs() / "pipeline.log").exists()      # the log opened there too
    assert paths.history_db().parent == paths.DATA

    monkeypatch.setattr(llm, "summarize", fake_summarize)
    src = transcript(paths.inbox(), "under-data.vtt")
    out = main.process_file(src)
    assert out.parent == paths.processed()
    assert out.is_relative_to(paths.DATA)


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
    (code / "config").mkdir(parents=True)
    (code / "processed").mkdir()
    (code / ".env").write_text("INBOX_DIR=/tmp/somewhere\n", encoding="utf-8")
    (code / "config" / "people.yaml").write_text("people: {}\n", encoding="utf-8")
    (code / "processed" / "old.json").write_text('{"title": "old"}', encoding="utf-8")

    copied = paths.migrate(code, data)

    assert len(copied) == 3, copied
    assert (data / ".env").read_text(encoding="utf-8") == "INBOX_DIR=/tmp/somewhere\n"
    assert (data / "config" / "people.yaml").exists()
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
