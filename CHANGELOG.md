# Changelog

What changed in each version of Meeting Notes, newest first. The reasons behind a change live in
its commit message; open problems live in the project note, not here.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow the commit
messages: `v0.MINOR.PATCH`, MINOR for a feature or substantial change, PATCH for a fix.

## v0.5.0 — 2026-10-31
### Added
- A main window: meeting list with search and status filters, the notes editor, Reprocess and
  Show transcript, Process a file…, settings (folder, watching, model, mail mode and account,
  recipients) and the job with a progress bar. React + Vite in `ui/`; a dithered night sky under one pane of glass.
- A Dock icon, an app menu with Edit (copy and paste work in the window), and *Open Meeting
  Notes* (⌘O) in the dropdown. The window opens at launch; the Dock icon reopens it.
### Changed
- The menu bar and the window act through one `Controller` (`controller.py`); the API gained
  state, settings, account, pick, folder, retry and reveal routes.
- `build.sh` and `run.sh` build the window with npm; Node.js is now a build requirement.

## v0.4.0 — 2026-10-31
### Added
- The parser reads old Teams `.vtt` / `.docx`, the current Teams `.docx` header, and
  speech-to-text exports with no speakers (`[start - end] text`); the model is told when nobody is named.
- Meetings left found or processing by a quit or a crash are queued again at start-up.
### Changed
- `main.py` split into `pipeline.py` (decide, summarise, the worker), `watcher.py` and the CLI.
  Importing a module no longer writes to the data folder. All settings and their defaults are
  in `paths.DEFAULTS`, which `.env.example` matches.
- The review window's API works by meeting, not by file path, and refuses other Host headers.
- Picking a failed or skipped file by hand summarises it. The fingerprint ignores punctuation.
### Removed
- `people.yaml` and its never-called recipient lookup; the `.env` mail password fallback.
### Fixed
- The same meeting as `.vtt` and `.docx` was summarised twice. `MIN_TURNS` and the other
  transcript-check settings in `.env` were ignored. A failed `codesign` no longer passes the build.

## v0.3.0 — 2026-10-06
### Added
- Settings ▸ Mail account…: email address, Gmail app password and a "Send test email" button.
  Gmail addresses imply smtp.gmail.com:465; `SMTP_HOST` / `SMTP_PORT` / `SMTP_FROM` still override.
- The mail password is stored in the macOS Keychain. A password left in `.env` is offered to be
  moved there once, at startup.
- Readable error for a rejected Gmail password, next to the server's own message.
### Changed
- `SMTP_PASSWORD` removed from `.env.example`.
- py2app build excludes `keyring.testing`, pytest, numpy and pygments (the bundle had grown to
  117 MB and lost its signature); `jaraco` modules are included by name.

## v0.2.0 — 2026-10-06
### Added
- History of meetings in `history.db`, keyed by a fingerprint of the conversation (speaker and
  text), not the file bytes. A meeting already seen is not summarised again.
- Statuses found → processing → ready → sent, plus failed and skipped. A job interrupted by Quit
  is queued again on the next start.
- Jobs wait for Ollama instead of failing when it is not running.
- Menu bar dropdown: status lines (Ollama, model, watched folder, current job and queue),
  Recent meetings with Open notes / Reprocess / Show transcript in Finder, Settings submenus
  (folder, model, mail mode, default recipients).
- CLI: `--parse-only` prints the fingerprint; `--once` refuses a known meeting unless `--force`.
### Changed
- One data folder for the app, `run.sh` and the CLI: `~/Library/Application Support/Meeting
  Notes`, overridable with `MEETING_NOTES_DATA`. Settings, notes and `people.yaml` are copied
  there once; nothing is overwritten.
- All paths and settings come from `paths.py` and are re-read after a change; no module keeps its
  own copy.
- The window shows one meeting only: editable notes, recipients and Send. Settings moved to the menu.
### Removed
- Moving transcripts into `processed/` and `failed/`: source files are never touched. `FAILED_DIR`.
- `Build Meeting Notes.command`.
### Fixed
- Edits to action items and assumptions in the window were not saved, and Send sent the unedited notes.
- Ollama was queried on the main thread every 20 s, which could freeze the menu.

## v0.1.2 — 2026-10-04
### Changed
- The watcher only detects stable files and queues them; a single worker runs the model, one job
  at a time, for watcher files and files picked by hand alike. Picking a file while busy queues it.
- Every menu, icon and notification update runs on the main thread.
### Fixed
- Changing a setting during processing started a second watcher on the same folder
  ("already scheduled").
- The window stopped refreshing after the first job.

## v0.1.1 — 2026-10-04
### Added
- pytest and ruff (`requirements-dev.txt`, `pyproject.toml`); parser tests moved to `tests/`.
### Removed
- Unused `UI_PORT` setting and two unused imports.

## v0.1.0 — 2026-09-23
### Added
- First version: folder watcher, Teams `.vtt` / `.docx` parser with a "is this a transcript"
  check, local Ollama summary with a JSON schema and map-reduce for long meetings, review window,
  Outlook draft or Gmail SMTP sending, menu bar app and py2app build.
