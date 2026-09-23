"""Where the code lives versus where the user's data lives.

Running from the repo the two are the same folder, which is convenient while
developing. Inside a built .app they must not be: the bundle is read-only and may
sit in /Applications, so everything the app writes goes to Application Support
instead and survives replacing the app with a newer build.
"""
import os
import shutil
import sys
from pathlib import Path

BUNDLED = bool(getattr(sys, "frozen", False))

if BUNDLED:
    # py2app puts imported modules inside a zip, so __file__ is useless here.
    # RESOURCEPATH is the bundle's Contents/Resources.
    CODE = Path(os.environ.get(
        "RESOURCEPATH", Path(sys.executable).resolve().parent.parent / "Resources"))
    DATA = Path.home() / "Library" / "Application Support" / "Meeting Notes"
else:
    CODE = Path(__file__).resolve().parent
    DATA = CODE

ENV_FILE = DATA / ".env"
PEOPLE_FILE = DATA / "config" / "people.yaml"


def seed():
    """First run inside a bundle: lay down the files the user is meant to edit."""
    if not BUNDLED:
        return
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "config").mkdir(parents=True, exist_ok=True)
    for src, dst in ((CODE / ".env.example", ENV_FILE),
                     (CODE / "config" / "people.yaml", PEOPLE_FILE)):
        if src.exists() and not dst.exists():
            shutil.copy(src, dst)
    if ENV_FILE.exists():
        # A fresh install has no folder set, and Downloads is where Teams lands.
        text = ENV_FILE.read_text(encoding="utf-8")
        if "INBOX_DIR=./inbox" in text:
            ENV_FILE.write_text(
                text.replace("INBOX_DIR=./inbox", f"INBOX_DIR={Path.home() / 'Downloads'}"),
                encoding="utf-8")


seed()
