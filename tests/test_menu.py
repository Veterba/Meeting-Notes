"""The dropdown, as data. Clicking cannot be tested headless; this is everything
that can be: the titles, the marks, the ticks and the order.
"""
from pathlib import Path

import history
import llm
import main
import menu
from helpers import fake_summarize, wait_until

FACTS = {
    "ollama_up": True, "model": "qwen3.5:9b", "model_ready": True,
    "models": ["qwen3.5:9b", "qwen3:14b"], "installed": ["qwen3.5:9b"],
    "watching": True, "folder": Path.home() / "Downloads", "mail_mode": "smtp",
    "job": {"active": False, "queued": 0, "stage": "", "file": "", "i": 0, "n": 0},
    "meetings": [],
}

MEETING = {"hash": "a" * 64, "title": "Sprint review", "date": "2026-09-23",
           "status": "ready", "notes": "/notes/sprint.json",
           "source": str(Path.home() / "Downloads" / "sprint.vtt"), "error": ""}


def titles(items):
    return ["-" if menu.is_separator(i) else i.title for i in items]


def find(items, title):
    return next(i for i in items if i.title == title)


# ---------------------------------------------------------------- status lines

def test_the_status_lines_say_what_is_going_on():
    assert menu.status_lines(FACTS) == [
        "● Ollama: running",
        "Model: qwen3.5:9b",
        "Watching: ~/Downloads",
        "Now: Idle",
    ]


def test_the_status_lines_say_when_things_are_not_going_on():
    facts = dict(FACTS, ollama_up=False, model_ready=False, watching=False)
    assert menu.status_lines(facts) == [
        "○ Ollama: not responding",
        "Model: qwen3.5:9b  (not downloaded)",
        "Watching: paused",
        "Now: Idle",
    ]


def test_the_now_line_follows_the_worker():
    def line(**job):
        return menu.job_line({"active": True, "file": "sprint.vtt", "i": 0, "n": 0,
                              "queued": 0, **job})

    assert line(stage="map", i=2, n=5) == "sprint.vtt — part 2/5"
    assert line(stage="reduce") == "sprint.vtt — writing the notes"
    assert line(stage="waiting") == "sprint.vtt — waiting for Ollama"
    assert line(stage="pull", i=300, n=5600) == "sprint.vtt — downloading 300/5600 MB"
    assert line(stage="map", i=1, n=2, queued=3) == "sprint.vtt — part 1/2 · 3 waiting"
    assert menu.job_line({"active": False, "queued": 2}) == "2 waiting"
    assert menu.job_line({}) == "Idle"


# ---------------------------------------------------------------- the whole tree

def test_the_dropdown_is_in_the_agreed_order():
    assert titles(menu.tree(FACTS)) == [
        "● Ollama: running", "Model: qwen3.5:9b", "Watching: ~/Downloads", "Now: Idle",
        "-",
        "Recent meetings", "Process a file…", "Watch the folder",
        "-",
        "Settings",
        "-",
        "Quit",
    ]


def test_only_the_top_four_lines_are_status_lines():
    tree = menu.tree(FACTS)
    assert [i.role for i in tree[:4]] == ["status"] * 4
    assert all(i.role != "status" for i in tree[4:])
    # A status line has no action, which is what greys it out.
    assert all(i.action is None for i in tree[:4])


def test_watch_the_folder_is_ticked_when_it_is_on():
    assert find(menu.tree(FACTS), "Watch the folder").checked is True
    assert find(menu.tree(dict(FACTS, watching=False)),
               "Watch the folder").checked is False


# ---------------------------------------------------------------- meetings

def test_a_meeting_offers_what_can_be_done_to_it():
    items = menu.meeting_items([MEETING])

    assert titles(items) == ["● Sprint review — 23 Sep"]
    assert titles(items[0].children) == ["Open notes…", "Reprocess",
                                         "Show transcript in Finder"]
    assert [c.arg for c in items[0].children] == [MEETING["hash"]] * 3


def test_every_status_has_its_own_mark():
    marks = {}
    for status in ("found", "processing", "ready", "sent", "failed"):
        marks[status] = menu.meeting_label(dict(MEETING, status=status))[0]
    assert marks == {"found": "·", "processing": "…", "ready": "●",
                     "sent": "✓", "failed": "✕"}


def test_a_meeting_with_no_notes_shows_why_instead_of_opening_them():
    failed = dict(MEETING, status="failed", notes="",
                  error="ValueError: model returned no valid JSON")
    children = titles(menu.meeting_items([failed])[0].children)

    assert "Open notes…" not in children
    assert children[0].startswith("ValueError: model returned no valid JSON")
    assert children[1:] == ["Reprocess", "Show transcript in Finder"]


def test_meetings_keep_the_order_they_came_in():
    second = dict(MEETING, hash="b" * 64, title="Payment gateway", date="2026-09-22")
    assert titles(menu.meeting_items([MEETING, second])) == [
        "● Sprint review — 23 Sep", "● Payment gateway — 22 Sep"]


def test_two_meetings_that_read_the_same_are_told_apart():
    """rumps keys a menu by its titles, so a repeat would never be drawn."""
    same = dict(MEETING, hash="b" * 64)
    assert titles(menu.meeting_items([MEETING, same])) == [
        "● Sprint review — 23 Sep", "● Sprint review — 23 Sep (2)"]


def test_a_long_title_is_cut_and_a_missing_one_falls_back_to_the_file():
    long_title = dict(MEETING, title="Quarterly planning for the payments platform "
                                     "and everything attached to it")
    assert len(menu.meeting_label(long_title)) < 60
    assert menu.meeting_label(long_title).endswith("— 23 Sep")
    assert "…" in menu.meeting_label(long_title)
    assert menu.meeting_label(dict(MEETING, title="")).startswith("● sprint.vtt")


def test_nothing_yet_when_there_are_no_meetings():
    items = menu.meeting_items([])
    assert titles(items) == ["nothing yet"]
    assert items[0].action is None


def test_the_date_is_shown_the_short_way():
    assert menu.day("2026-09-23") == "23 Sep"
    assert menu.day("2026-10-06T15:40:24") == "06 Oct"
    assert menu.day("") == ""
    assert menu.day("not a date") == "not a date"


# ---------------------------------------------------------------- settings

def test_the_settings_submenu_ticks_what_is_in_use():
    settings = menu.settings_items(FACTS)

    assert titles(settings) == ["Folder…", "Model", "Mail", "Default recipients…"]
    models = find(settings, "Model").children
    assert [(m.title, m.checked) for m in models] == [
        ("qwen3.5:9b", True), ("qwen3:14b  (download)", False)]
    assert [m.arg for m in models] == ["qwen3.5:9b", "qwen3:14b"]
    mail = find(settings, "Mail").children
    assert [(m.title, m.checked, m.arg) for m in mail] == [
        ("Draft in Outlook", False, "outlook_draft"),
        ("Send over SMTP", True, "smtp")]


def test_the_model_list_says_so_when_ollama_is_not_answering():
    items = menu.model_items(dict(FACTS, models=[], installed=[]))
    assert titles(items) == ["Ollama is not answering"]
    assert items[0].action is None


# ---------------------------------------------------------------- from the db

def test_the_meetings_in_the_menu_come_from_the_history(box, worker, monkeypatch,
                                                        transcript):
    monkeypatch.setattr(llm, "summarize", fake_summarize)
    monkeypatch.setattr(llm, "ollama_up", lambda: True)
    for name in ("first.vtt", "second.vtt"):
        decision = main.submit(transcript(box.inbox, name))
        assert wait_until(lambda d=decision: history.find(d["hash"])["status"] == "ready")

    items = menu.meeting_items(history.listing(7))

    assert titles(items) == ["● second.vtt — 06 Oct", "● first.vtt — 06 Oct"]
    assert all(c.arg for i in items for c in i.children)
