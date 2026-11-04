"""Parse meeting transcripts (.vtt / .srt / .docx / .txt / .md) into speaker turns.

Each file is one of a few known shapes, and the shape is decided first, from the
whole file, before any line is read as a speaker or as text. Reading line by line
with every rule switched on at once is what turned dates and sentences with a
colon into speakers. The shapes:

  subtitles (.vtt, .srt), cue by cue:
    voice      current and old Teams: <v Name>text</v>, any timecode style
    named      Zoom and most others: "Name: text" in the cue
    subtitles  no speakers at all: one turn per cue
  lines (.docx paragraphs, .txt and .md lines):
    heading    current Teams and Otter: "Name   0:03", or Zoom's "[Name] 10:15:32",
               then the text below
    timecode   old Teams: "0:0:0.0 --> 0:0:13.370", the name, the text
    segments   speech-to-text exports: "[00:00:00.660 - 00:00:13.080] text",
               no speakers at all
    named      "Name: text" lines, a timestamp allowed around the name (Google
               Meet, Granola, chat logs), or plain text with no speakers
"""
import re
from dataclasses import dataclass
from pathlib import Path

import paths

UNKNOWN = "Unknown"

# "00:00:01.234 --> 00:00:05.678", and the old Teams "0:0:0.0 --> 0:0:13.370"
RE_TIMECODE = re.compile(r"^\d{1,2}:\d{1,2}(:\d{1,2})?([.,]\d{1,3})?\s*-->")
# "<v Ivan Petrov>text</v>", the closing tag optional, a class allowed: <v.loud …>
RE_VOICE = re.compile(r"<v(?:\.[^\s>]*)?\s+([^>]+?)\s*>(.*?)(?:</v>)?\s*$", re.S)
# A clock time or an offset: "0:03", "00:01:23.660", "10:15:32 AM"
STAMP = r"(?:\d{1,2}:)?\d{1,2}:\d{2}(?:[.,]\d+)?(?:\s*[AP]M)?"
# "Ivan Petrov: text" (colon after a short name)
RE_NAMED = re.compile(r"^([^:]{2,60}?):\s+(.+)$", re.S)
# "Ivan Petrov (00:01:23): text" — the colons of the time would stop RE_NAMED
RE_NAMED_STAMPED = re.compile(rf"^(.{{2,60}}?)\s*[(\[]{STAMP}[)\]]\s*:\s+(.+)$", re.S | re.I)
# "[00:01:23] " or "00:01:23 - " in front of a named line
RE_LEAD_STAMP = re.compile(rf"^[(\[]?{STAMP}[)\]]?\s*[-–]?\s*", re.I)
# A line that is only a time: Google Meet writes one every few minutes
RE_STAMP_LINE = re.compile(rf"^[(\[]?{STAMP}[)\]]?$", re.I)
# The speaker heading of a current Teams .docx, Otter and Zoom: "Ivan Petrov<tab>12:34",
# "Ivan Petrov - 0:03", "Ivan Petrov (0:03)", "[Ivan Petrov] 10:15:32". The separator
# may be a tab, several spaces or a single one.
RE_HEADING = re.compile(rf"^\[?(.{{2,60}}?)\]?[\s ]+(?:[-–|][\s ]*)?\(?{STAMP}\)?$", re.I)
# "[00:00:00.660 - 00:00:13.080] text", or whisper's "[00:00.000 --> 00:05.000] text"
RE_SEGMENT = re.compile(rf"^\[\s*{STAMP}\s*(?:-->|[-–—])\s*{STAMP}\s*\]\s*(.*)$")
# Lines the app writes, not anybody in the meeting: "Ivan Petrov started
# transcription" (Teams), "Meeting ended after 00:31:44" (Google Meet)
RE_SERVICE = re.compile(r"^(.{2,60} (started|stopped) transcription|meeting ended after\b.*)$",
                        re.I)
RE_TAGS = re.compile(r"<[^>]+>")
RE_CUE_GUID = re.compile(r"[0-9a-fA-F\-]{8,}(/\d+-\d+)?")

SERVICE_WORDS = {"WEBVTT", "NOTE", "STYLE", "REGION"}


@dataclass(frozen=True)
class Parsed:
    """What a file said, turn by turn.

    `named` is False for a format that never names its speakers: every turn is
    then UNKNOWN, kept one segment per turn, and the model is told so.
    """
    turns: list                     # [(speaker, text), ...]
    named: bool = True
    shape: str = ""                 # which of the shapes above it was read as


def _clean(text: str) -> str:
    text = RE_TAGS.sub("", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    return re.sub(r"\s+", " ", text).strip()


def _merge(turns):
    """Join consecutive turns by the same speaker."""
    merged = []
    for speaker, text in turns:
        if not text:
            continue
        if merged and merged[-1][0] == speaker:
            prev = merged[-1][1]
            sep = " " if prev.endswith((".", "!", "?", ",", "…")) else ". "
            merged[-1][1] = (prev + sep + text).strip()
        else:
            merged.append([speaker, text])
    return [(s, t) for s, t in merged]


def _looks_like_name(s: str) -> bool:
    """A person's name is short, mostly capitalised, and has no sentence in it.

    "Ivan Petrov", "Alexey Gromov (Guest)", "Speaker 2" pass. "Both halves were
    verified" — a sentence the colon rule picked up — does not, and neither does a
    date such as "October 6, 2026": a number is allowed only as the last word.
    """
    s = s.strip()
    words = s.split()
    if not s or len(s) > 48 or not 1 <= len(words) <= 4 or not any(c.isalpha() for c in s):
        return False
    if any(c in s for c in ".!?;"):
        return False
    if any(any(c.isdigit() for c in w) for w in words[:-1]):
        return False
    if any(c.isdigit() for c in words[-1]) and not words[-1].isdigit():
        return False
    # "Ludwig van Beethoven" passes; "Let's meet at" does not.
    return sum(not w[0].islower() for w in words) * 2 >= len(words)


# ---------------------------------------------------------------- reading

EXTENSIONS = {".vtt", ".srt", ".docx", ".txt", ".md"}


def is_candidate(path) -> bool:
    """A file the app would read: a transcript extension, and not a hidden or an
    Office lock file ("~$Meeting.docx")."""
    p = Path(path)
    return p.suffix.lower() in EXTENSIONS and not p.name.startswith((".", "~"))


def files_in(folder) -> list:
    """Every candidate file in a folder and its subfolders, hidden folders left out."""
    folder = Path(folder)
    return sorted(p for p in folder.rglob("*")
                  if p.is_file() and is_candidate(p)
                  and not any(part.startswith(".") for part in p.relative_to(folder).parts))


def _read_text(path) -> str:
    """A text file's contents. Windows tools write UTF-16 with a byte order mark."""
    data = Path(path).read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    return data.decode("utf-8-sig", errors="replace")


def _voice(line: str):
    """(speaker, text) of a "<v Name>text</v>" line, or None."""
    m = RE_VOICE.match(line)
    return (_clean(m.group(1)), _clean(m.group(2))) if m else None


def _named(line: str):
    """(speaker, text) of a "Name: text" line, or None. A time next to the name,
    in front of it or in brackets after it, is not part of the name."""
    bare = RE_LEAD_STAMP.sub("", line)
    m = RE_NAMED_STAMPED.match(bare) or RE_NAMED.match(bare)
    if not m or bare.startswith("http"):
        return None
    return _clean(m.group(1)), _clean(m.group(2))


# ---------------------------------------------------------------- subtitles

def _cues(path: Path) -> list:
    """The text lines of each cue of a .vtt or .srt, without the header,
    timecodes and cue ids."""
    lines = _read_text(path).splitlines()
    cues = []
    for i, raw in enumerate(lines):
        s = raw.strip()
        # Everything before the first timecode is the file header: WEBVTT,
        # X-TIMESTAMP-MAP, NOTE blocks and so on.
        if RE_TIMECODE.match(s):
            cues.append([])
            continue
        if not cues or not s:
            continue
        # The line right before a timecode is a cue identifier, whatever its shape.
        if i + 1 < len(lines) and RE_TIMECODE.match(lines[i + 1].strip()):
            continue
        if s.split()[0].upper() in SERVICE_WORDS or s.isdigit() or RE_CUE_GUID.fullmatch(s):
            continue
        cues[-1].append(s)
    return [c for c in cues if c]


def parse_vtt(path: Path) -> Parsed:
    """A .vtt or an .srt: voice tags, "Name: text" cues, or no speakers at all."""
    cues = _cues(path)
    body = [line for cue in cues for line in cue]
    if any(_voice(line) for line in body):
        shape, said = "voice", _voice
    elif sum(bool((n := _named(line)) and _looks_like_name(n[0])) for line in body) >= 2:
        shape, said = "named", _named
    else:
        # Nobody named: a speech-to-text export, or a film. One turn per cue,
        # like the segments of a .docx export.
        turns = [(UNKNOWN, _clean(" ".join(cue))) for cue in cues]
        return Parsed([t for t in turns if t[1]], named=False, shape="subtitles")
    turns = []
    for line in body:
        turn = said(line)
        if turn:
            turns.append(list(turn))
        elif turns:                         # the rest of the turn on the next line
            turns[-1][1] = f"{turns[-1][1]} {_clean(line)}".strip()
        else:
            turns.append([UNKNOWN, _clean(line)])
    return Parsed(_merge(turns), shape=shape)


# ---------------------------------------------------------------- lines

def _docx_lines(path: Path) -> list:
    """Every non-empty line, a paragraph's soft line breaks included."""
    from docx import Document

    lines = []
    for para in Document(str(path)).paragraphs:
        lines += [s.strip() for s in para.text.split("\n") if s.strip()]
    return lines


def _text_lines(path: Path) -> list:
    """Every non-empty line of a .txt or .md, without Markdown's headings,
    quote and list marks, and bold: "**Ivan:** text" is "Ivan: text"."""
    lines = []
    for raw in _read_text(path).splitlines():
        s = re.sub(r"^(#+|>|[-*+])\s+", "", raw.strip())
        s = s.replace("**", "").replace("__", "").strip()
        if s:
            lines.append(s)
    return lines


def _heading(line: str):
    """The speaker of a "Name   0:03" heading, or None."""
    m = RE_HEADING.match(line)
    return _clean(m.group(1)) if m and _looks_like_name(m.group(1)) else None


def _said(lines: list) -> list:
    """The lines without those nobody said: the app's own notes and bare times."""
    return [line for line in lines if not (RE_SERVICE.match(line) or RE_STAMP_LINE.match(line))]


def _shape(lines: list) -> str:
    def count(test):
        return sum(1 for line in lines if test(line))

    if count(RE_SEGMENT.match) >= 2:
        return "segments"
    if count(RE_TIMECODE.match) >= 2:
        return "timecode"
    if count(_heading) >= 2:
        return "heading"
    return "named"


def _segments(lines):
    turns = []
    for line in lines:
        m = RE_SEGMENT.match(line)
        if m:
            turns.append((UNKNOWN, _clean(m.group(1))))
        elif turns:                         # a segment wrapped onto a second line
            turns[-1] = (UNKNOWN, f"{turns[-1][1]} {_clean(line)}")
        # Anything before the first segment is the template's own header.
    # One turn per segment, never merged: a speech-to-text export is long, and
    # the map step needs somewhere to cut it.
    return [t for t in turns if t[1]]


def _timecode(lines):
    turns, speaker, expect_name = [], None, False
    for line in lines:
        if RE_TIMECODE.match(line):
            expect_name = True
            continue
        if expect_name and _looks_like_name(line):
            speaker, expect_name = _clean(line), False
            continue
        expect_name = False
        if speaker:
            turns.append([speaker, _clean(line)])
    return _merge(turns)


def _headings(lines):
    turns, speaker = [], None
    for line in _said(lines):
        name = _heading(line)
        if name:
            speaker = name
        elif speaker:                       # before the first heading: the header
            turns.append([speaker, _clean(line)])
    return _merge(turns)


def _named_lines(lines):
    turns, speaker, header = [], None, []
    for line in _said(lines):
        turn = _named(line)
        if turn:
            speaker = turn[0]
            turns.append(list(turn))
        elif speaker:
            turns.append([speaker, _clean(line)])
        else:
            header.append(_clean(line))     # the title and date, before any speaker
    # No speaker found at all — hand back the text rather than lose it.
    if not turns and header:
        turns = [[UNKNOWN, " ".join(header)]]
    return _merge(turns)


LINE_SHAPES = {"segments": _segments, "timecode": _timecode,
               "heading": _headings, "named": _named_lines}


def parse_lines(lines: list) -> Parsed:
    """A .docx, .txt or .md, as its lines."""
    shape = _shape(lines)
    return Parsed(LINE_SHAPES[shape](lines), named=shape != "segments", shape=shape)


# ---------------------------------------------------------------- the rest

def parse(path) -> Parsed:
    path = Path(path)
    ext = path.suffix.lower()
    if ext in (".vtt", ".srt"):
        return parse_vtt(path)
    if ext == ".docx":
        return parse_lines(_docx_lines(path))
    if ext in (".txt", ".md"):
        lines = _text_lines(path)
        # Some apps save their subtitles as .txt.
        if lines and lines[0].upper().startswith("WEBVTT"):
            return parse_vtt(path)
        return parse_lines(lines)
    raise ValueError(f"Unsupported format: {ext}")


def to_text(turns) -> str:
    """The transcript as the model reads it. A turn with no speaker is just text."""
    return "\n".join(t if s == UNKNOWN else f"{s}: {t}" for s, t in turns)


def speakers(turns) -> list:
    seen = []
    for s, _ in turns:
        if s not in seen:
            seen.append(s)
    return seen


def looks_like_transcript(parsed: Parsed) -> tuple:
    """Is this a meeting transcript, or just some .vtt/.docx that landed nearby?

    Returns (ok, reason). Cheap: runs on the already parsed turns, no LLM call.

    A speech-to-text export is recognised by its shape alone and only has to be
    long enough. Subtitles with no names are turned down: they could be a film.
    Anything else has to pass three signals, in order of how much they
    discriminate:
      1. Somebody is speaking at all. A movie subtitle file carries no speaker
         tags and an ordinary document has no speaker headings, so both collapse
         into one Unknown turn.
      2. Speakers recur. A conversation alternates between a few people; a
         document gives every "speaker" exactly one turn, because what the colon
         rule matched was a section heading, not a name.
      3. The names are shaped like names, not like sentences.
    """
    turns = parsed.turns
    if not turns:
        return False, "no dialogue lines found"

    # The minimum shape a file must have before minutes of model time go into it.
    min_turns, min_chars = int(paths.number("MIN_TURNS")), int(paths.number("MIN_CHARS"))
    min_per_speaker, min_name_ratio = (paths.number("MIN_TURNS_PER_SPEAKER"),
                                       paths.number("MIN_NAME_RATIO"))

    distinct = [s for s in speakers(turns) if s != UNKNOWN]
    if parsed.named and not distinct:
        return False, "no speaker names found"
    if parsed.shape == "subtitles":
        # Without names, a film's subtitles look the same as a call's.
        return False, "subtitles with no speaker names, as likely a film as a meeting"

    if len(turns) < min_turns:
        return False, f"only {len(turns)} turns, need at least {min_turns}"

    size = len(to_text(turns))
    if size < min_chars:
        return False, f"only {size} characters, need at least {min_chars}"

    if not parsed.named:
        return True, ""

    per_speaker = len(turns) / len(distinct)
    if per_speaker < min_per_speaker:
        return False, (f"{len(distinct)} speakers for {len(turns)} turns — nobody "
                       f"speaks twice, so this reads as a document, not a conversation")

    name_ratio = sum(_looks_like_name(s) for s in distinct) / len(distinct)
    if name_ratio < min_name_ratio:
        return False, (f"only {name_ratio:.0%} of the detected speakers look like "
                       f"names, the rest look like headings")

    return True, ""
