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

from dotenv import dotenv_values, set_key, unset_key

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
PEOPLE_FILE = DATA / "config" / "people.yaml"

_values = {}


# ---------------------------------------------------------------- settings

def reload():
    """Re-read .env. Called at import and after every save_setting."""
    global _values
    _values = dotenv_values(ENV_FILE) if ENV_FILE.exists() else {}


def setting(key: str, default: str = "") -> str:
    """The environment first, then .env, then the default.

    The environment wins so that a test or a one-off CLI run can point the app
    somewhere harmless without touching the user's own settings.
    """
    value = os.environ.get(key)
    if value is None:
        value = _values.get(key)
    return default if value is None or value == "" else value


def setting_int(key: str, default: int) -> int:
    try:
        return int(float(setting(key, str(default))))
    except ValueError:
        return default


def setting_float(key: str, default: float) -> float:
    try:
        return float(setting(key, str(default)))
    except ValueError:
        return default


def flag(key: str, default: bool = True) -> bool:
    return setting(key, "1" if default else "0").strip().lower() \
        not in ("0", "false", "no", "off", "")


def save_setting(key: str, value: str):
    """Write one setting to .env and make it current for this process."""
    DATA.mkdir(parents=True, exist_ok=True)
    ENV_FILE.touch(exist_ok=True)
    set_key(str(ENV_FILE), key, value, quote_mode="auto")
    # An environment override would otherwise silently win over what was just
    # saved, and the menu would appear to ignore the change.
    os.environ.pop(key, None)
    reload()


def remove_setting(key: str):
    """Take a setting out of .env for good — how the SMTP password leaves it."""
    if ENV_FILE.exists():
        unset_key(str(ENV_FILE), key)
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


# ---------------------------------------------------------------- first start

def migrate(code: Path = None, data: Path = None) -> list:
    """Bring across whatever the data folder does not have yet.

    Each of the three things is copied only when it is missing on the data side,
    so this is safe on every start: nothing is overwritten, and nothing is ever
    removed from the code folder. In particular an existing `.env` is left exactly
    as it is — when there are two of them, which one wins is the user's decision,
    not this function's.

    Returns lines for the log. Logging is not configured this early, and the
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

    people = code / "config" / "people.yaml"
    if people.exists() and not (data / "config" / "people.yaml").exists():
        _dir(data / "config")
        shutil.copy(people, data / "config" / "people.yaml")
        done.append(f"copied {people.name}")

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


MIGRATED = migrate()
reload()
