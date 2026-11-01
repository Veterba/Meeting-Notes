"""Where the code lives, where the user's data lives, and what the settings say.

The code folder is wherever the app was built from; the data folder is always
~/Library/Application Support/Meeting Notes, bundled or not. One data folder means
the built app, a run from source and the CLI share the same settings, notes and
logs — having two of them was the bug this replaces.

Every module asks this one for a folder or a setting instead of keeping a copy
read at import: the menu, the window and the CLI all change the same `.env`, and
a change has to be visible to the others without restarting anything.

MEETING_NOTES_DATA overrides the data folder, which is how the tests and an
isolated run from source stay away from the real one.
"""
import os
import shutil
import sys
from pathlib import Path

from dotenv import dotenv_values, set_key

BUNDLED = bool(getattr(sys, "frozen", False))

if BUNDLED:
    # py2app puts imported modules inside a zip, so __file__ is useless here.
    # RESOURCEPATH is the bundle's Contents/Resources.
    CODE = Path(os.environ.get(
        "RESOURCEPATH", Path(sys.executable).resolve().parent.parent / "Resources"))
else:
    CODE = Path(__file__).resolve().parent

DATA = Path(os.environ.get("MEETING_NOTES_DATA")
            or Path.home() / "Library" / "Application Support" / "Meeting Notes"
            ).expanduser()

ENV_FILE = DATA / ".env"

_values = {}

# Every setting the app reads, and its value when neither the environment nor
# .env gives one. .env.example documents exactly these keys; a test keeps the
# two in step, so a knob cannot be documented and then quietly ignored.
DEFAULTS = {
    # folders and the watcher
    "INBOX_DIR": "", "PROCESSED_DIR": "", "WATCH_ENABLED": "1",
    "STABLE_SECONDS": 30, "POLL_SECONDS": 5, "SCAN_MAX_AGE_HOURS": 24,
    # is this a transcript at all
    "MIN_TURNS": 4, "MIN_CHARS": 400, "MIN_TURNS_PER_SPEAKER": 2.0, "MIN_NAME_RATIO": 0.6,
    # Ollama
    "OLLAMA_URL": "http://localhost:11434", "OLLAMA_MODEL": "",
    "RAM_FOR_BIG": 24, "RAM_FOR_MID": 20, "NUM_CTX": 32768, "OLLAMA_RETRY_SECONDS": 15,
    "TEMPERATURE": 0.2, "MAP_THRESHOLD_TOKENS": 6000, "CHUNK_TOKENS": 5000,
    "OVERLAP_TURNS": 2, "REQUEST_TIMEOUT": 1800,
    # recording
    "WHISPER_MODEL": "", "WHISPER_LANGUAGE": "auto",
    # deleted meetings stay in the archive this many days: 7, 30 or 90
    "ARCHIVE_DAYS": 30,
    # mail
    "MAIL_MODE": "outlook_draft", "RECIPIENTS": "", "SMTP_USER": "",
    "SMTP_HOST": "", "SMTP_PORT": 465, "SMTP_FROM": "",
}


# ---------------------------------------------------------------- settings

def reload():
    """Re-read .env. Called at import and after every save_setting."""
    global _values
    _values = dotenv_values(ENV_FILE) if ENV_FILE.exists() else {}


def setting(key: str) -> str:
    """The environment first, then .env, then DEFAULTS.

    The environment wins so that a test or a one-off CLI run can point the app
    somewhere harmless without touching the user's own settings.
    """
    value = os.environ.get(key)
    if value is None:
        value = _values.get(key)
    return str(DEFAULTS[key]) if value is None or value == "" else value


def number(key: str) -> float:
    """A numeric setting. Something unreadable in .env means the default."""
    try:
        return float(setting(key))
    except ValueError:
        return float(DEFAULTS[key])


def flag(key: str) -> bool:
    return setting(key).strip().lower() not in ("0", "false", "no", "off", "")


def save_setting(key: str, value: str):
    """Write one setting to .env and make it current for this process."""
    if key not in DEFAULTS:
        raise KeyError(f"unknown setting {key}")
    DATA.mkdir(parents=True, exist_ok=True)
    ENV_FILE.touch(exist_ok=True)
    set_key(str(ENV_FILE), key, value, quote_mode="auto")
    # An environment override would otherwise silently win over what was just
    # saved, and the menu would appear to ignore the change.
    os.environ.pop(key, None)
    reload()


# ---------------------------------------------------------------- folders

def _dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


def _folder(key: str, name: str) -> Path:
    """A folder named by a setting.

    A relative path is relative to the data folder, never to the current
    directory: an app launched from Finder has / as its working directory, and
    `INBOX_DIR=./inbox` would then mean /inbox.
    """
    raw = setting(key)
    p = Path(raw).expanduser() if raw else DATA / name
    return _dir(p if p.is_absolute() else DATA / p)


def inbox() -> Path:
    return _folder("INBOX_DIR", "inbox")


def processed() -> Path:
    return _folder("PROCESSED_DIR", "processed")


def logs() -> Path:
    return _dir(DATA / "logs")


def history_db() -> Path:
    return _dir(DATA) / "history.db"


def recordings() -> Path:
    return _dir(DATA / "recordings")


def recorder_binary() -> Path:
    """The Swift helper that records a call. Inside the bundle it sits next to
    the app's own executable; from source, build.sh / run.sh compile it into build/."""
    if BUNDLED:
        return Path(sys.executable).resolve().parent / "mn-recorder"
    return CODE / "build" / "mn-recorder"


# ---------------------------------------------------------------- first start

def migrate(code: Path = None, data: Path = None) -> list:
    """Bring across whatever the data folder does not have yet.

    Each of the two things is copied only when it is missing on the data side,
    so this is safe on every start: nothing is overwritten, and nothing is ever
    removed from the code folder. In particular an existing `.env` is left exactly
    as it is — when there are two of them, which one wins is the user's decision,
    not this function's.

    Called by main.init, never at import. Returns lines for the log; the
    contents of .env are nobody's business, so only names and counts are said.
    """
    code = code or CODE
    data = data or DATA
    done = []
    _dir(data)

    if not (data / ".env").exists():
        source = code / ".env" if (code / ".env").exists() else code / ".env.example"
        if source.exists():
            shutil.copy(source, data / ".env")
            done.append(f"settings: copied {source.name} to {data / '.env'}")
            if source.name == ".env.example":
                # Nothing is set yet, and Downloads is where Teams lands.
                text = (data / ".env").read_text(encoding="utf-8")
                if "INBOX_DIR=./inbox" in text:
                    (data / ".env").write_text(
                        text.replace("INBOX_DIR=./inbox",
                                     f"INBOX_DIR={Path.home() / 'Downloads'}"),
                        encoding="utf-8")
                    done.append("settings: pointed INBOX_DIR at ~/Downloads")

    # Only ever on the very first run, so a meeting deleted here does not come
    # back from the code folder on the next start.
    old_notes = code / "processed"
    if old_notes.is_dir() and not (data / "processed").exists():
        target = _dir(data / "processed")
        copied = 0
        for f in sorted(old_notes.iterdir()):
            if f.is_file():
                shutil.copy2(f, target / f.name)
                copied += 1
        done.append(f"copied {copied} file(s) from {old_notes} to {target}")
    return done


reload()
