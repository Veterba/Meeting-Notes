"""Deleted meetings and recordings: kept in DATA/archive for a while, then gone.

Deleting never destroys anything at once. A meeting's notes and, for a recorded
call, its whole recording folder move into one archive entry, with a manifest
saying where each came from; ARCHIVE_DAYS later (7, 30 or 90) the entry is
removed for good. Until then it can be restored.

The meeting's history row stays, marked archived. That row is what stops the
watcher from summarising a downloaded transcript again, so it outlives the
archive. A downloaded transcript itself is never moved: it is the user's file.
"""
import json
import re
import secrets
import shutil
import time
from datetime import datetime
from pathlib import Path

import history
import paths

CHOICES = (7, 30, 90)                   # days, as the settings offer them
MANIFEST = "manifest.json"
RE_ENTRY = re.compile(r"^\d{4}-\d\d-\d\d \d\d-\d\d-\d\d [0-9a-f]{6}$")
RE_RECORDING = re.compile(r"^\d{4}-\d\d-\d\d \d\d-\d\d-\d\d$")


def root() -> Path:
    path = paths.DATA / "archive"
    path.mkdir(parents=True, exist_ok=True)
    return path


def days() -> int:
    n = int(paths.number("ARCHIVE_DAYS"))
    return n if n in CHOICES else 30


def recording_of(source) -> Path:
    """The recording folder a transcript belongs to, or None for a downloaded one."""
    rec = paths.recordings()
    src = Path(source)
    if not src.is_relative_to(rec) or src == rec:
        return None
    return rec / src.relative_to(rec).parts[0]


def recording(name: str) -> Path:
    """A recording folder by its name, refusing anything that is not one."""
    folder = paths.recordings() / name
    if not RE_RECORDING.match(name) or not folder.is_dir():
        raise KeyError(f"no recording {name}")
    return folder


def _title(row: dict) -> str:
    notes = Path(row["notes"]) if row.get("notes") else None
    if notes and notes.exists():
        try:
            return json.loads(notes.read_text(encoding="utf-8")).get("title") or notes.stem
        except (OSError, ValueError):
            pass
    return Path(row["source"]).name


def _store(kind: str, title: str, things: list, **extra) -> str:
    """Move things into a new entry and write its manifest. Returns the entry id."""
    entry_id = f"{datetime.now():%Y-%m-%d %H-%M-%S} {secrets.token_hex(3)}"
    entry = root() / entry_id
    entry.mkdir()
    moved = []
    for thing in things:
        shutil.move(str(thing), entry / thing.name)
        moved.append({"from": str(thing), "name": thing.name})
    manifest = {"kind": kind, "title": title, "archived_at": time.time(), "moved": moved, **extra}
    (entry / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
    return entry_id


def archive_meeting(meeting: str) -> str:
    row = history.find(meeting)
    if not row or row["status"] == "archived":
        raise KeyError("no such meeting")
    things = []
    folder = recording_of(row["source"])
    if folder and folder.exists():
        things.append(folder)
    notes = Path(row["notes"]) if row["notes"] else None
    if notes and notes.exists() and not (folder and notes.is_relative_to(folder)):
        things.append(notes)
    entry_id = _store("meeting", _title(row), things, hash=meeting, status=row["status"])
    history.set_status(meeting, "archived")
    return entry_id


def archive_recording(name: str) -> str:
    """A recording that never became a meeting: nothing was recognised in it."""
    folder = recording(name)
    return _store("recording", f"Call {name}", [folder])


def _entry(entry_id: str) -> tuple:
    entry = root() / entry_id
    if not RE_ENTRY.match(entry_id) or not (entry / MANIFEST).exists():
        raise KeyError(f"no archived item {entry_id}")
    return entry, json.loads((entry / MANIFEST).read_text(encoding="utf-8"))


def restore(entry_id: str):
    """Put everything back where it was, and the meeting back in the list."""
    entry, manifest = _entry(entry_id)
    for item in manifest["moved"]:
        if Path(item["from"]).exists():
            raise RuntimeError(f"something is already at {item['from']}")
    for item in manifest["moved"]:
        target = Path(item["from"])
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(entry / item["name"]), target)
    if manifest.get("hash"):
        history.set_status(manifest["hash"], manifest["status"])
    shutil.rmtree(entry)


def entries() -> list:
    """What is in the archive, newest first, with when each goes for good."""
    keep = days() * 86400
    out = []
    for entry in sorted(root().iterdir(), reverse=True):
        try:
            _, manifest = _entry(entry.name)
        except (KeyError, ValueError, OSError):
            continue
        out.append({"id": entry.name, "kind": manifest["kind"], "title": manifest["title"],
                    "archived_at": manifest["archived_at"],
                    "deletes_at": manifest["archived_at"] + keep})
    return out


def purge(now: float = None) -> int:
    """Remove for good what has been in the archive longer than ARCHIVE_DAYS."""
    now = now or time.time()
    gone = 0
    for item in entries():
        if item["deletes_at"] <= now:
            shutil.rmtree(root() / item["id"], ignore_errors=True)
            gone += 1
    return gone
