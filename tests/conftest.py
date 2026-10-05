"""Keep the tests away from the real folders.

`main` reads INBOX_DIR / PROCESSED_DIR / FAILED_DIR at import and creates them,
and INBOX_DIR in a working .env is usually ~/Downloads. These are set before any
test imports main, and load_dotenv does not override what is already in the
environment, so the tests never see the real folders.
"""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="meeting-notes-tests-")
for _key in ("INBOX_DIR", "PROCESSED_DIR", "FAILED_DIR"):
    os.environ[_key] = os.path.join(_TMP, _key.split("_")[0].lower())
