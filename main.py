"""Command line, and the start-up every entry point shares. Produces JSON only —
email is always sent from the UI."""
import argparse
import logging
import sys
import time
from pathlib import Path

import history
import llm
import parser as tparser
import paths
import pipeline
from pipeline import process, register
from watcher import Watcher

log = logging.getLogger("pipeline")


def init():
    """Bring the data folder up to date and start the log. Once, at start-up.

    Kept out of import time on purpose: importing a module must never write to
    the data folder, or a test or a quick `python -c` could touch the real one.
    """
    done = paths.migrate()
    paths.reload()
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    form = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    for handler in (logging.FileHandler(paths.logs() / "pipeline.log", encoding="utf-8"),
                    logging.StreamHandler(sys.stdout)):
        handler.setFormatter(form)
        root.addHandler(handler)
    for line in done:                   # what the first start brought across
        log.info("Data folder: %s", line)


def main():
    ap = argparse.ArgumentParser(description="Teams transcript -> meeting notes (JSON)")
    ap.add_argument("--once", metavar="FILE", help="process one file and exit")
    ap.add_argument("--parse-only", metavar="FILE",
                    help="show how the transcript parsed, without calling the model")
    ap.add_argument("--model", help="override the Ollama model")
    ap.add_argument("--force", action="store_true",
                    help="summarise again a meeting already in the history")
    args = ap.parse_args()

    if args.parse_only:
        src = Path(args.parse_only).expanduser().resolve()
        if not src.exists():
            sys.exit(f"No such file: {src}")
        parsed = tparser.parse(src)
        turns = parsed.turns
        if not turns:
            sys.exit("No dialogue lines recognised — format not supported")
        ok, reason = tparser.looks_like_transcript(parsed)
        text = tparser.to_text(turns)
        meeting = history.fingerprint(turns)
        known = history.find(meeting)
        print(f"Verdict:  {'looks like a transcript' if ok else 'NOT a transcript — ' + reason}")
        print(f"Meeting:  {meeting[:16]}"
              + (f"  (already {known['status']})" if known else "  (new)"))
        print(f"Shape:    {parsed.shape}")
        print(f"Turns:    {len(turns)}")
        print("Speakers: " + (", ".join(tparser.speakers(turns)) if parsed.named
                              else "none named in this format"))
        tokens = llm.estimate_tokens(text)
        print(f"Size:     {len(text)} chars, ~{tokens} tokens")
        print("Mode:     " + ("map-reduce" if tokens > paths.number("MAP_THRESHOLD_TOKENS")
                              else "single pass"))
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

        def show(stage, i, n):
            print(f"  {stage} {i}/{n}", flush=True)

        # The same history as the watcher: a meeting already summarised is not
        # summarised again unless you say so.
        decision = register(src, force=args.force)
        if decision.error:
            sys.exit(decision.error)
        if not decision.queued:
            print(f"{src.name}: {decision.note}"
                  + (f"\n{decision.notes}" if decision.notes else ""))
            print("Add --force to summarise it again.")
            return
        if not llm.OLLAMA.ensure():
            sys.exit("Ollama could not be started: brew install ollama, or see logs/ollama.log")
        try:
            out = process(decision.transcript, model=args.model, progress_cb=show)
        finally:
            llm.OLLAMA.release()
        print(out.read_text(encoding="utf-8"))
    else:
        # Headless watcher: the same queue and worker the menu bar app uses.
        pipeline.resume()
        watcher = Watcher()
        watcher.start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            log.info("Stopping")
        finally:
            watcher.stop()
            pipeline.WORKER.stop(timeout=1)
            llm.OLLAMA.release()


if __name__ == "__main__":
    init()
    main()
