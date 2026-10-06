# Meeting Notes

A Teams transcript becomes a set of meeting notes and an email draft, entirely on
your Mac. The model is Ollama running locally — no cloud API, no Microsoft Graph,
no data leaving the machine. It lives in the menu bar: drop a transcript in a
folder, and a few minutes later the notes are waiting for you to read and send.

Nothing is ever emailed without you pressing the button.

## Requirements

- macOS 12 or later, Apple silicon or Intel
- [Ollama](https://ollama.com) — `brew install ollama`
- Python 3.10+ (macOS ships one; `brew install python` also works)
- Outlook for Mac, if you want drafts. A Gmail app password works instead.

## Install

```bash
git clone <this repo> meeting-notes
cd meeting-notes

brew install ollama
brew services start ollama

./build.sh --install
```

`build.sh` creates the virtualenv, installs the dependencies, draws the icons and
produces **Meeting Notes.app** (~46 MB). With `--install` it also copies the app
into `/Applications`. Without the flag the app is left in `dist/`.

Then open it — from Launchpad, Spotlight, or:

```bash
open "/Applications/Meeting Notes.app"
```

**The icon appears in the menu bar.** There is no Dock icon and no window until
you ask for one: the app is a menu bar item by design (`LSUIElement`).

### First run

Click the icon. Everything is in the dropdown: the top four lines say what is
going on, the rest is what you can do about it. Under **Settings** set the folder
to watch (`~/Downloads` to begin with, which is where Teams puts a downloaded
transcript), the model, whether the mail is an Outlook draft or an SMTP send, and
the people every set of notes goes to.

### Where everything lives

```
~/Library/Application Support/Meeting Notes/
  .env               settings — the menu, the window and the CLI all write here
  config/people.yaml
  processed/         the notes, one JSON per meeting
  history.db         every meeting it has seen, by fingerprint
  logs/pipeline.log
```

**One data folder, always** — the built app, `./run.sh` and `python main.py`
share it, so a setting changed in one is seen by the others and replacing the app
with a newer build keeps everything. The first start copies `.env`,
`config/people.yaml` and `processed/` out of the repo if they are there, and never
deletes the originals.

`MEETING_NOTES_DATA=/some/where` moves the whole folder, which is how the tests
and an experimental run stay away from the real one:

```bash
MEETING_NOTES_DATA=/tmp/mn-dev ./run.sh
```

The first time a draft is created, macOS asks for permission to control Outlook:
System Settings → Privacy & Security → Automation.

### Pull the model

The app offers to download it, or do it yourself:

```bash
ollama pull qwen3.5:9b      # or whichever model the table below lands on
```

The model is picked from installed memory (`sysctl hw.memsize`): **24 GB and up →
`qwen3:30b-a3b`**, **20 GB and up → `qwen3:14b`**, otherwise **`qwen3.5:9b`**.
The thresholds are `RAM_FOR_BIG` / `RAM_FOR_MID` in `.env`.

Those thresholds come from measurement, not from the size of the model file.
What has to fit in memory is the weights **plus the KV cache for `NUM_CTX`**.
On a 16 GB machine `qwen3:14b` at `num_ctx=32768` takes 12 GB resident, swaps,
and turns a three-turn meeting into a three-minute wait instead of fifteen
seconds. There is one check: `ollama ps`, the `PROCESSOR` column. Anything other
than `100% GPU` means layers were offloaded to the CPU, and it will crawl.

## Running from source

For hacking on it, skip the build:

```bash
./run.sh
```

Same setup steps, but it runs `menubar.py` straight from the working tree. This
is for development only — the app proper is the build. It is the same settings,
notes and logs as the built app (see *Where everything lives*), so set
`MEETING_NOTES_DATA` if you do not want to share them.

The checks live behind `requirements-dev.txt` — pytest and ruff, which the app
itself does not need:

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q       # the fixture suite
.venv/bin/ruff check .              # lint
```

<details>
<summary>The pieces on their own, for debugging</summary>

```bash
source .venv/bin/activate

python main.py                                  # just the watcher, no icon
python main.py --once ~/Downloads/x.vtt         # one file, JSON to stdout
python main.py --once ~/Downloads/x.vtt --force # ...even if already in history
python main.py --parse-only ~/Downloads/x.vtt   # parse only, never calls the model
python -m pytest -q                             # the fixture suite

python main.py --parse-only examples/sample-meeting-ru.vtt   # try the sample
python main.py --once      examples/sample-meeting-ru.vtt
python assets/make_icon.py --icns               # redraw every icon
```

`--parse-only` prints the verdict, the speakers, the turn count and a token
estimate without touching Ollama. It is the first thing to run on a transcript
from an unfamiliar source.
</details>

## Try it without a real meeting

`examples/` holds a synthetic fifteen-minute Russian standup and the notes the
pipeline produced from it, so you can see the output shape before wiring up
Teams:

```bash
python main.py --parse-only examples/sample-meeting-ru.vtt   # instant, no model
python main.py --once      examples/sample-meeting-ru.vtt    # ~90 s on qwen3.5:9b
```

Or drop `examples/sample-meeting-ru.vtt` into the watched folder and let the app
pick it up.

## How the app is put together

```
menubar.py   the status bar item, the owner of everything
 ├ menu.py    what the dropdown says and in what order — a plain function
 ├ server.py  a loopback HTTP API on a random port, gated by a startup token
 ├ window.py  an NSWindow with a WKWebView pointed at that API
 └ ui/        the page itself — plain HTML and JavaScript, no framework
main.py      watcher, queue and CLI   parser.py  .vtt/.docx into speaker turns
llm.py       Ollama, map-reduce       mailer.py  Jinja2, Outlook, SMTP
history.py   what it has seen         menu.py    the dropdown, as data
paths.py     folders and settings
```

The UI is served from `127.0.0.1` on a port the OS assigns, and every API call
carries a token generated at startup, so nothing else on the machine can drive
the pipeline through it.

## The menu bar app

The dropdown is the app. It is a widget, not a window with a menu attached:

```
● Ollama: running
Model: qwen3.5:9b
Watching: ~/Downloads                  (or: paused)
Now: sprint-review.vtt — part 2/5      (or: Idle, or: 2 waiting)
────────────────────────────────────
Recent meetings ▸  ✓ Sprint review — 23 Sep
                   ● Payment gateway — 22 Sep
                   ✕ standup.vtt — 21 Sep
                        ▸ Open notes… / Reprocess / Show transcript in Finder
Process a file…
✓ Watch the folder
────────────────────────────────────
Settings ▸  Folder… / Model ▸ / Mail ▸ / Default recipients…
────────────────────────────────────
Quit
```

- the **four status lines** are grey and not clickable. The last two change while
  a job runs, every two seconds; asking Ollama what it has happens every twenty,
  off the main thread, so the menu never waits for it;
- **Recent meetings** is the last seven rows of `history.db`, newest first, with
  the status as a mark: `·` waiting, `…` working, `●` ready, `✓` sent, `✕` failed.
  A meeting that failed shows why, and offers *Reprocess* instead of notes;
- **Process a file…** picks a transcript by hand. It skips the is-it-a-transcript
  check, because you chose it, and joins the same queue as anything the watcher
  finds, so it waits its turn rather than being refused. A meeting that has been
  summarised before opens its notes instead of being summarised again;
- **Watch the folder** is the on/off switch. Off, the icon turns into the same
  page with a line struck through it, so the menu bar says at a glance whether
  anything is running. A job already in progress is left to finish;
- **Settings** writes to `.env`, which the menu, the window and the CLI all read,
  so a change takes effect at once and with no restart. The model list ticks what
  is installed; picking one that is not downloads it. **Mail account…** is the
  sheet described below.

**The window opens for one meeting and nothing else**: its notes, editable, the
recipients, and one button. It has no settings and no file list — those are the
menu's job. A notification arrives when notes are ready, and the email still does
not go anywhere until you press the button.

The icons live in `assets/icon.png` and `assets/icon-paused.png`; redraw both
with `python assets/make_icon.py`.

## The mail account

**Settings ▸ Mail account…** is a sheet with two fields and a *Send test email*
button, and that is the whole of it:

- the **address** the notes are sent from. It goes into `.env` as `SMTP_USER`;
- its **app password**. It goes into the **macOS Keychain** — service
  `Meeting Notes`, account the address — and never into a file, a log or an API
  response. For Gmail, make one at
  [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords);
  your normal password will be refused.

Saving the sheet also sets `MAIL_MODE=smtp`. **Send test email** sends a short
message from the account to itself and shows what happened in the sheet: on
failure, the server's exact words plus a line about what to do about them.

A **Gmail or googlemail address implies the rest** — `smtp.gmail.com`, port 465,
and itself as the sender — which is why the sheet asks for nothing else. For any
other provider, `SMTP_HOST`, `SMTP_PORT` and `SMTP_FROM` in `.env` are the way to
say so; they are deliberately not in the menu. Without a host, nothing is tried
and the error says which setting is missing.

If an older `.env` still has `SMTP_PASSWORD` in it, it keeps working as a
fallback, and the app offers **once** to move it into the Keychain and take it out
of the file.

## Where transcripts come from

Teams hands the transcript over as a download, so it lands in `~/Downloads`.
`INBOX_DIR` can point straight there — nothing else has to be moved by hand.

Two guards make that safe:

**Age.** On startup the watcher only considers files newer than
`SCAN_MAX_AGE_HOURS` (a day by default). Whatever has been sitting in Downloads
for weeks is left alone. Files that arrive later are always picked up.

**Is it a transcript at all?** Not every `.vtt` is a meeting — it could be
subtitles for a film — and most `.docx` files are just documents. Before any
model time is spent, `parser.looks_like_transcript()` checks three things:

1. *Somebody is speaking.* Subtitles carry no speaker tags and a document has no
   speaker headings, so both collapse into a single `Unknown` turn.
2. *Speakers recur.* A conversation alternates between a few people. A document
   gives every "speaker" exactly one turn, because what the `Name:` rule matched
   was a section heading. This is the signal that does most of the work: a real
   meeting runs 61 turns across 3 speakers, a report 14 turns across 14.
3. *The names look like names.* "Ivan Petrov" passes; "Both halves were verified
   in the running application" does not.

A file that fails is **left exactly where it is** — never moved, never deleted.
It belongs to someone else. The reason goes to `logs/pipeline.log`, and
`--parse-only` prints the same verdict.

Thresholds: `MIN_TURNS`, `MIN_CHARS`, `MIN_TURNS_PER_SPEAKER`, `MIN_NAME_RATIO`.
Picking a file by hand — in the editor or via **Process a file…** — skips the
check entirely, because the choice was explicit.

## How it works

1. **Parsing** (`parser.py`) — timecodes, cue identifiers and header lines are
   stripped from `.vtt` and `.docx`; consecutive turns by the same speaker are
   joined into `Speaker: text`. The model understands "Maria said this, whole"
   far better than twenty fragments, and it costs fewer tokens.
2. **The model** (`llm.py`) — Ollama `/api/generate`, `num_ctx=32768`,
   `temperature=0.2`, the JSON schema passed in `format` so generation is
   constrained by grammar rather than by request. Qwen3's thinking mode is
   switched off twice over (`think=false` and `/no_think`) because either alone
   fails on some builds. A transcript longer than ~6000 tokens goes through
   map-reduce: ~5000-token chunks cut strictly on turn boundaries with a one-to-
   two-turn overlap. Invalid JSON gets one retry at `temperature=0`, and then
   the meeting is marked `failed` in the history, with the error in its row.
3. **The email** (`mailer.py`) — Jinja2 over `templates/email.html`.
   `MAIL_MODE=outlook_draft` creates a draft in Outlook for Mac through
   `osascript` and does not send it; `MAIL_MODE=smtp` sends through SMTP with an
   app password (see *The mail account*). Recipients: `RECIPIENTS` from `.env` plus participants
   matched in `config/people.yaml`. Both take a bare address or
   `Name <address@example.com>`, separated by commas or semicolons, deduplicated
   by address.

Prompts live in `prompts/map.md` and `prompts/reduce.md` — edit those, not the
code. Their central rule: invent nothing; anything inferred goes only into
`assumptions` and must carry a verbatim quote; answer in the language of the
meeting.

### Facts versus assumptions

This split is the point of the whole thing, and it lives in the prompts.

A model told to "summarise" always fills in the gaps. It will write "the team
agreed to postpone" when one person agreed out loud. In finished prose that is
almost impossible to spot, because it reads exactly like a real fact.

So `summary`, `decisions` and `action_items` may only contain what was said out
loud, and everything the model worked out for itself goes into `assumptions`,
each with the `evidence_quote` it rests on and a `confidence` level. Reading the
notes, you read the first part as fact and the assumptions as a to-check list.

### Notes schema

```jsonc
{
  "title": "…",
  "date": "2026-09-23",
  "participants": ["…"],
  "summary": "five to seven sentences",
  "decisions": ["…"],
  "action_items":   [{ "owner": "…", "task": "…", "deadline": "by Thursday|null" }],
  "assumptions":    [{ "statement": "…", "confidence": "low|medium|high", "evidence_quote": "…" }],
  "open_questions": ["…"]
}
```

`deadline` keeps the words that were spoken — "by Thursday", "30 September" —
and is never converted to a calendar date. Small models get that arithmetic
wrong, and a confidently wrong date is worse than a vague one.

Every meeting's JSON stays in `processed/`, which makes it easy to edit a prompt
and diff the result over the same transcript.

### The history

`history.db` (sqlite) is the memory of what the app has seen. A meeting is keyed
by a **fingerprint**: the sha256 of its parsed turns — speaker and text,
whitespace collapsed, lower case — not of the bytes of the file. The same
conversation as `.docx` and as `.vtt`, or `Meeting.docx` and `Meeting (1).docx`,
is one meeting and is summarised once.

One row per meeting: `hash`, `source`, `found_at`, `status`, `error`, `notes`,
`recipients`, `sent_at`, `model`. The status runs `found` → `processing` →
`ready` → `sent`, or stops at `failed`; a file that parsed but is not a meeting
is `skipped`, which is only there so the watcher stops reconsidering it.

**Transcripts are never moved, renamed or rewritten.** The app reads them where
they are and the history remembers them, so your Downloads folder is left exactly
as it was. A meeting that failed can be tried again from the history — *Try
again* in the window — and `--once --force` summarises a known meeting afresh.

If Ollama is not running when a job comes up, the job **waits** for it instead of
failing, and starts as soon as it answers (`OLLAMA_RETRY_SECONDS`).

## Tests

```bash
.venv/bin/python -m pytest -q
```

`tests/test_parser.py` has twelve fixtures. Eight that must be accepted: classic Teams VTT with GUID cue
identifiers, VTT without the closing `</v>`, VTT with an `X-TIMESTAMP-MAP` and
`NOTE` header, a turn split across consecutive cues, a guest suffix, flat
`Name: text`, and `.docx` with tab and with space separators. Four that must be
rejected: film subtitles, a report, a meeting agenda and plain prose. Beside it,
`test_jobs.py` covers the queue and the watcher, `test_history.py` the
fingerprint and the history flow, and `test_paths.py` the data folder.

If a transcript ever parses wrong, add its shape to `tests/test_parser.py` and fix the
regexes in `parser.py` until the suite is green again.

## When something breaks

Three places to look:

In `~/Library/Application Support/Meeting Notes/`:

- `logs/pipeline.log` — every run, with timestamps
- `history.db` — one row per meeting, with the error text of whatever failed:
  `sqlite3 history.db 'select status, error, source from meetings'`

| Symptom | Cause |
|---|---|
| `no dialogue lines could be extracted` | format not recognised — look at the first lines and add a rule to `parser.py` |
| ignored, "nobody speaks twice" | the file is a document, not a transcript; use **Process a file…** to force it |
| `model returned no valid JSON after 2 attempts` | model too small or chunk too large — raise the model or lower `CHUNK_TOKENS` |
| a meeting sits at `waiting for Ollama` | Ollama is not running — the job starts by itself once it is |
| notes only cover the start of the meeting | `NUM_CTX` too low |
| `osascript: … not allowed` | Outlook automation not permitted yet |

Notes files are never overwritten by a different meeting: on a name collision
`_unique()` appends `HHMMSS`. Summarising the *same* meeting again does replace
its own notes.

## Start it at login

With the app installed, this is a normal macOS setting — no plist needed:

System Settings → General → Login Items & Extensions → **Open at Login** → `+` →
`/Applications/Meeting Notes.app`

<details>
<summary>launchd, if you run from source instead of the app</summary>

`~/Library/LaunchAgents/local.meetingnotes.plist`, with your own paths. Point it
at `main.py` rather than `menubar.py` for a headless watcher with no icon:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>              <string>local.meetingnotes</string>
  <key>ProgramArguments</key>
  <array>
    <string>/PATH/TO/meeting-notes/.venv/bin/python</string>
    <string>/PATH/TO/meeting-notes/menubar.py</string>
  </array>
  <key>WorkingDirectory</key>   <string>/PATH/TO/meeting-notes</string>
  <key>RunAtLoad</key>          <true/>
  <key>KeepAlive</key>          <true/>
  <key>StandardOutPath</key>    <string>/PATH/TO/meeting-notes/logs/launchd.out.log</string>
  <key>StandardErrorPath</key>  <string>/PATH/TO/meeting-notes/logs/launchd.err.log</string>
</dict>
</plist>
```

```bash
launchctl load  ~/Library/LaunchAgents/local.meetingnotes.plist
launchctl unload ~/Library/LaunchAgents/local.meetingnotes.plist
```

Use the venv's Python, not the system one, or the dependencies will not be found.
</details>

## Notes on the build

The app is signed ad-hoc (`codesign --sign -`), which is enough for it to keep
its Automation and notification permissions between launches on your own machine.
It is **not** notarised, so on someone else's Mac Gatekeeper will object on first
open — right-click the app and choose Open, once.

Rebuilding is safe: user data lives in Application Support, not in the bundle.

## Repository layout

```
menubar.py  menu.py  server.py  window.py  ui/   the app: icon, dropdown, API, window, page
main.py     parser.py  llm.py     mailer.py   the pipeline, watcher and queue
history.py                        meetings by fingerprint (sqlite)
paths.py                          code and data folders, and the settings
prompts/    templates/ config/    the bits you are meant to edit
assets/     make_icon.py          icons, drawn at build time
examples/                         a sample transcript and its notes
build.sh    setup.py              produces Meeting Notes.app
run.sh                            run from source, no build
tests/                            the fixture suite (pytest)
requirements-dev.txt  pyproject.toml   pytest and ruff, and their settings
```

Not committed: `.env`, `.venv/`, `build/`, `dist/`, and the leftover `logs/`,
`inbox/`, `processed/`, `failed/` from before the data folder moved. Settings and
meeting notes never leave the machine, and never land in git.

No licence file is included — add whichever one your team uses before making the
repository public.
