"""Folder watcher plus CLI. Produces JSON only — email is always sent from the UI."""
import argparse
import json
import logging
import os
import shutil
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from paths import DATA, ENV_FILE

load_dotenv(ENV_FILE)

BASE = DATA
INBOX_DIR = Path(os.getenv("INBOX_DIR", DATA / "inbox")).expanduser().resolve()
PROCESSED_DIR = Path(os.getenv("PROCESSED_DIR", DATA / "processed")).expanduser().resolve()
FAILED_DIR = Path(os.getenv("FAILED_DIR", DATA / "failed")).expanduser().resolve()
LOG_DIR = DATA / "logs"
STABLE_SECONDS = int(os.getenv("STABLE_SECONDS", "30"))
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "5"))
# On startup, ignore whatever has been sitting in the folder for a while. This is
# what makes it safe to point INBOX_DIR at ~/Downloads: old files stay untouched.
SCAN_MAX_AGE_HOURS = float(os.getenv("SCAN_MAX_AGE_HOURS", "24"))
EXTENSIONS = {".vtt", ".docx"}

for d in (INBOX_DIR, PROCESSED_DIR, FAILED_DIR, LOG_DIR):
    d.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_DIR / "pipeline.log", encoding="utf-8"),
              logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("pipeline")


class NotATranscript(Exception):
    """The file parsed fine but is not a meeting transcript. Leave it alone."""


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    stamp = datetime.now().strftime("%H%M%S")
    return path.with_name(f"{path.stem}-{stamp}{path.suffix}")


def process_file(src: Path, model: str = None, progress_cb=None,
                 move: bool = True, require_transcript: bool = False) -> Path:
    """Transcript -> processed/<name>.json.

    On failure the source moves to failed/ with a .log beside it and the exception
    is re-raised. A file rejected as "not a transcript" is never moved: it is
    somebody else's document that happened to land in the watched folder.
    """
    import parser as tparser
    from llm import summarize

    src = Path(src)
    log.info("Processing %s", src.name)
    try:
        turns = tparser.parse(src)
        if not turns:
            raise ValueError("no dialogue lines could be extracted")

        if require_transcript:
            ok, reason = tparser.looks_like_transcript(turns)
            if not ok:
                log.info("Skipping %s: %s", src.name, reason)
                raise NotATranscript(reason)

        meta = {"source": src.name,
                "date": datetime.fromtimestamp(src.stat().st_mtime).strftime("%Y-%m-%d")}
        data = summarize(turns, meta, model=model, progress_cb=progress_cb)

        out = _unique(PROCESSED_DIR / f"{src.stem}.json")
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

        if move:
            shutil.move(str(src), str(_unique(PROCESSED_DIR / src.name)))
        log.info("Done: %s (%d turns)", out.name, len(turns))
        return out

    except NotATranscript:
        raise                       # not ours — leave the file where it is
    except Exception as e:
        log.error("Failed on %s: %s", src.name, e)
        if move and src.exists():
            dst = _unique(FAILED_DIR / src.name)
            shutil.move(str(src), str(dst))
            stem = dst.stem
        else:
            stem = src.stem
        (FAILED_DIR / f"{stem}.log").write_text(
            f"{datetime.now().isoformat()}\n{src}\n\n{traceback.format_exc()}",
            encoding="utf-8")
        raise


# ---------------------------------------------------------------- watcher

def watch(inbox=None, model=None, on_done=None, on_error=None, on_skip=None,
          stop_event=None):
    """Watch a folder. Blocks the calling thread until stop_event is set.

    The callbacks let the menu bar app show notifications; the watcher itself
    knows nothing about any UI.

    A file is only picked up once its size has stopped changing for
    STABLE_SECONDS. OneDrive makes a file appear immediately and fills it in
    afterwards, and a truncated transcript produces a silently wrong summary,
    which is worse than a crash.
    """
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    inbox = Path(inbox or INBOX_DIR)
    inbox.mkdir(parents=True, exist_ok=True)
    pending = {}  # path -> (size, when the size last changed)

    def note(path):
        p = Path(path)
        if p.suffix.lower() in EXTENSIONS and not p.name.startswith((".", "~")):
            pending.setdefault(str(p), (-1, time.time()))

    class Handler(FileSystemEventHandler):
        def on_created(self, e):
            if not e.is_directory:
                note(e.src_path)

        def on_modified(self, e):
            if not e.is_directory:
                note(e.src_path)

        def on_moved(self, e):
            if not e.is_directory:
                note(e.dest_path)

    cutoff = time.time() - SCAN_MAX_AGE_HOURS * 3600
    skipped = 0
    for f in inbox.iterdir():
        if not f.is_file() or f.suffix.lower() not in EXTENSIONS:
            continue
        if f.stat().st_mtime >= cutoff:
            note(f)
        else:
            skipped += 1
    if skipped:
        log.info("Ignored %d file(s) older than %g h", skipped, SCAN_MAX_AGE_HOURS)

    observer = Observer()
    observer.schedule(Handler(), str(inbox), recursive=False)
    observer.start()
    log.info("Watching %s (stability %d s)", inbox, STABLE_SECONDS)

    try:
        while True:
            # Wait on the event rather than sleeping, so a stop is acted on at
            # once. A slow shutdown means the next observer tries to watch the
            # same path while this one still holds it, and watchdog refuses.
            if stop_event:
                if stop_event.wait(POLL_SECONDS):
                    break
            else:
                time.sleep(POLL_SECONDS)
            for key in list(pending):
                p = Path(key)
                if not p.exists():
                    pending.pop(key, None)
                    continue
                size, since = pending[key]
                now = time.time()
                current = p.stat().st_size
                if current != size:
                    pending[key] = (current, now)      # still growing — restart the timer
                elif now - since >= STABLE_SECONDS:
                    pending.pop(key, None)
                    try:
                        out = process_file(p, model=model, require_transcript=True)
                        if on_done:
                            on_done(p, out)
                    except NotATranscript as e:
                        if on_skip:
                            on_skip(p, e)
                    except Exception as e:            # already in failed/ and the log
                        if on_error:
                            on_error(p, e)
    except KeyboardInterrupt:
        log.info("Stopping")
    finally:
        observer.stop()
        observer.join()


def main():
    ap = argparse.ArgumentParser(description="Teams transcript -> meeting notes (JSON)")
    ap.add_argument("--once", metavar="FILE", help="process one file and exit")
    ap.add_argument("--parse-only", metavar="FILE",
                    help="show how the transcript parsed, without calling the model")
    ap.add_argument("--model", help="override the Ollama model")
    args = ap.parse_args()

    if args.parse_only:
        import parser as tparser
        src = Path(args.parse_only).expanduser().resolve()
        if not src.exists():
            sys.exit(f"No such file: {src}")
        turns = tparser.parse(src)
        if not turns:
            sys.exit("No dialogue lines recognised — format not supported")
        ok, reason = tparser.looks_like_transcript(turns)
        text = tparser.to_text(turns)
        print(f"Verdict:  {'looks like a transcript' if ok else 'NOT a transcript — ' + reason}")
        print(f"Turns:    {len(turns)}")
        print(f"Speakers: {', '.join(tparser.speakers(turns))}")
        print(f"Size:     {len(text)} chars, ~{len(text) // 3} tokens")
        print(f"Mode:     {'map-reduce' if len(text) // 3 > 6000 else 'single pass'}")
        print("-" * 60)
        for speaker, line in turns[:5]:
            print(f"{speaker}: {line[:160]}{'…' if len(line) > 160 else ''}")
        if len(turns) > 5:
            print(f"... {len(turns) - 5} more turns")
        return

    if args.once:
        src = Path(args.once).expanduser().resolve()
        if not src.exists():
            sys.exit(f"No such file: {src}")
        # A file outside INBOX_DIR is a one-off run — do not relocate it.
        move = INBOX_DIR in src.parents

        def show(stage, i, n):
            print(f"  {stage} {i}/{n}", flush=True)

        out = process_file(src, model=args.model, progress_cb=show, move=move)
        print(out.read_text(encoding="utf-8"))
    else:
        watch()


if __name__ == "__main__":
    main()
