"""The dropdown as data: what it says, and in what order.

rumps is handed the result of `tree()` and nothing else. Keeping the titles, the
marks and the order in a plain function is what makes the part that is easy to get
wrong testable without a menu bar — see tests/test_menu.py.
"""
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# What a meeting's status looks like in the list.
MARK = {"found": "·", "processing": "…", "ready": "●", "sent": "✓", "failed": "✕"}
MAIL_MODES = (("outlook_draft", "Draft in Outlook"), ("smtp", "Send over SMTP"))
STAGES = {"map": "part {i}/{n}", "reduce": "writing the notes",
          "pull": "downloading {i}/{n} MB", "waiting": "waiting for Ollama",
          "starting": "starting"}
TITLE_LIMIT = 42


@dataclass
class Item:
    """One line of the menu. `action` is the name of a method on the menu bar app."""
    title: str
    action: str = None
    arg: object = None            # what to hand the action: a hash, a model name…
    children: tuple = ()
    checked: bool = None          # a tick, for one of a set of choices
    key: str = None               # ⌘ shortcut
    role: str = ""                # "status" for the grey lines at the top


SEPARATOR = Item("-")


def is_separator(item: Item) -> bool:
    return item.title == SEPARATOR.title


def short_path(p) -> str:
    p = str(p)
    home = str(Path.home())
    return "~" + p[len(home):] if p.startswith(home) else p


def day(value: str) -> str:
    """2026-09-23 -> 23 Sep. Anything unparseable comes back as it was."""
    try:
        return datetime.fromisoformat(value).strftime("%d %b")
    except (TypeError, ValueError):
        return value or ""


def job_line(job: dict) -> str:
    """The Now: line — whatever the one worker is doing, whoever started it."""
    queued = job.get("queued") or 0
    if not job.get("active"):
        return f"{queued} waiting" if queued else "Idle"
    stage = STAGES.get(job.get("stage") or "", "working")
    stage = stage.format(i=job.get("i") or 0, n=job.get("n") or 0)
    waiting = f" · {queued} waiting" if queued else ""
    return f"{job.get('file') or 'a meeting'} — {stage}{waiting}"


def status_lines(facts: dict) -> list:
    """The four grey lines. Two of them change while a job runs."""
    model = facts.get("model") or "—"
    return [
        ("● Ollama: running" if facts.get("ollama_up") else "○ Ollama: not responding"),
        f"Model: {model}" + ("" if facts.get("model_ready") else "  (not downloaded)"),
        (f"Watching: {short_path(facts.get('folder'))}" if facts.get("watching")
         else "Watching: paused"),
        f"Now: {job_line(facts.get('job') or {})}",
    ]


def meeting_label(meeting: dict) -> str:
    title = (meeting.get("title") or "").strip() or Path(meeting["source"]).name
    if len(title) > TITLE_LIMIT:
        title = title[:TITLE_LIMIT - 1].rstrip() + "…"
    mark = MARK.get(meeting.get("status"), "·")
    date = day(meeting.get("date"))
    return f"{mark} {title}" + (f" — {date}" if date else "")


def meeting_items(meetings) -> list:
    """One item per meeting, newest first, each with what can be done to it.

    rumps keys a menu by its titles, so two meetings that read the same have to be
    told apart or the second one would never appear.
    """
    if not meetings:
        return [Item("nothing yet")]
    items, seen = [], {}
    for meeting in meetings:
        label = meeting_label(meeting)
        seen[label] = seen.get(label, 0) + 1
        if seen[label] > 1:
            label = f"{label} ({seen[label]})"
        actions = []
        if meeting.get("notes"):
            actions.append(Item("Open notes…", action="open_notes", arg=meeting["hash"]))
        elif meeting.get("error"):
            actions.append(Item(meeting["error"][:60]))
        actions.append(Item("Reprocess", action="reprocess", arg=meeting["hash"]))
        actions.append(Item("Show transcript in Finder", action="reveal",
                            arg=meeting["hash"]))
        items.append(Item(label, children=tuple(actions)))
    return items


def model_items(facts: dict) -> list:
    """The models to choose from, the installed ones ticked."""
    installed = facts.get("installed") or []
    items = []
    for name in facts.get("models") or []:
        label = name if name in installed else f"{name}  (download)"
        items.append(Item(label, action="choose_model", arg=name,
                          checked=name == facts.get("model")))
    return items or [Item("Ollama is not answering")]


def settings_items(facts: dict) -> list:
    return [
        Item("Folder…", action="choose_folder"),
        Item("Model", children=tuple(model_items(facts))),
        Item("Mail", children=tuple(
            Item(label, action="choose_mail", arg=mode,
                 checked=facts.get("mail_mode") == mode)
            for mode, label in MAIL_MODES)),
        Item("Mail account…", action="mail_account"),
        Item("Default recipients…", action="edit_recipients"),
    ]


def tree(facts: dict) -> list:
    """The whole dropdown, top to bottom."""
    items = [Item(line, role="status") for line in status_lines(facts)]
    items += [
        SEPARATOR,
        Item("Recent meetings", children=tuple(meeting_items(facts.get("meetings")))),
        Item("Process a file…", action="pick_file"),
        Item("Watch the folder", action="toggle_watching",
             checked=bool(facts.get("watching"))),
        SEPARATOR,
        Item("Settings", children=tuple(settings_items(facts))),
        SEPARATOR,
        Item("Quit", action="quit", key="q"),
    ]
    return items
