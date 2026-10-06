"""What the app has already seen. One sqlite table at DATA/history.db.

A meeting is identified by what was said in it, not by the file it arrived in:
Teams hands out the same conversation as .docx and as .vtt, and a second download
is `Meeting (1).docx`. Both have to be recognised as the same meeting, so the key
is a hash of the parsed turns.

Every connection is opened and closed around one statement. The watcher thread,
the worker and the HTTP handlers all write here, and sqlite objects cannot be
shared between threads.
"""
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import paths

# Statuses, in the order a meeting moves through them:
#   found      the file is a new meeting, waiting for the worker
#   processing the model is running on it now
#   ready      notes written, waiting to be read and sent
#   sent       the email went out (or the draft was created)
#   failed     something broke; the error text is in the row
#   skipped    parsed, but not a meeting transcript — left alone on purpose
TERMINAL = ("ready", "sent", "failed", "skipped")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
    hash       TEXT PRIMARY KEY,
    source     TEXT NOT NULL,
    found_at   TEXT NOT NULL,
    status     TEXT NOT NULL,
    error      TEXT,
    notes      TEXT,
    recipients TEXT,
    sent_at    TEXT,
    model      TEXT
)
"""


def fingerprint(turns) -> str:
    """sha256 of the conversation itself, not of the bytes of the file.

    Speaker and text, whitespace collapsed, lower case. The same meeting in two
    formats parses to the same turns and so to the same hash; two different
    meetings never collide.
    """
    lines = (f"{' '.join(str(speaker).split()).lower()}: "
             f"{' '.join(str(text).split()).lower()}" for speaker, text in turns)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


@contextmanager
def _db():
    conn = sqlite3.connect(paths.history_db(), timeout=15)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def find(meeting: str) -> dict:
    with _db() as conn:
        row = conn.execute("SELECT * FROM meetings WHERE hash = ?", (meeting,)).fetchone()
    return dict(row) if row else None


def find_by_notes(notes_path: str) -> dict:
    """Which meeting a notes file belongs to — all the window knows is the path."""
    with _db() as conn:
        row = conn.execute("SELECT * FROM meetings WHERE notes = ? ORDER BY found_at DESC",
                           (str(notes_path),)).fetchone()
    return dict(row) if row else None


def record(meeting: str, source, status: str, error: str = None):
    """Insert a meeting, or move an existing one back to this status.

    found_at keeps the first sighting; the source path is updated, because the
    same meeting can turn up again somewhere else.
    """
    with _db() as conn:
        conn.execute(
            """INSERT INTO meetings (hash, source, found_at, status, error)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(hash) DO UPDATE SET
                   source = excluded.source,
                   status = excluded.status,
                   error  = excluded.error""",
            (meeting, str(source), _now(), status, error))


def set_status(meeting: str, status: str, **fields):
    fields["status"] = status
    columns = ", ".join(f"{k} = ?" for k in fields)
    with _db() as conn:
        conn.execute(f"UPDATE meetings SET {columns} WHERE hash = ?",
                     (*fields.values(), meeting))


def mark_sent(meeting: str, recipients):
    set_status(meeting, "sent", recipients=", ".join(recipients), sent_at=_now())


def listing(limit: int = 25) -> list:
    """Recent meetings as the menu and the window want them.

    The title and the date come out of the notes file when there is one, and from
    the transcript's name and the day it was found when there is not — a meeting
    that failed has no notes to read a title from.
    """
    items = []
    for row in recent(limit):
        title, date = Path(row["source"]).name, (row["found_at"] or "")[:10]
        notes = Path(row["notes"]) if row["notes"] else None
        if notes and notes.exists():
            try:
                data = json.loads(notes.read_text(encoding="utf-8"))
                title = data.get("title") or title
                date = data.get("date") or date
            except (OSError, ValueError):
                pass
        items.append({"hash": row["hash"], "title": title, "date": date,
                      "status": row["status"], "notes": row["notes"] or "",
                      "source": row["source"], "error": row["error"] or ""})
    return items


def recent(limit: int = 25) -> list:
    """Meetings, newest first. Skipped files are left out — they are not meetings,
    they only sit in the table so the watcher stops reconsidering them."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM meetings WHERE status != 'skipped' "
            "ORDER BY found_at DESC, rowid DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]
