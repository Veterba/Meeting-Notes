"""Parse meeting transcripts (.vtt / .docx) into speaker turns.

Each file is one of a few known shapes, and the shape is decided first, from the
whole file, before any line is read as a speaker or as text. Reading line by line
with every rule switched on at once is what turned dates and sentences with a
colon into speakers. The shapes:

  .vtt  voice      current and old Teams: <v Name>text</v>, any timecode style
  .vtt  named      no voice tags; "Name: text" lines, or no speakers at all
  .docx heading    current Teams: a header, then "Name   0:03" and the text below
  .docx timecode   old Teams: "0:0:0.0 --> 0:0:13.370", the name, the text
  .docx segments   speech-to-text exports: "[00:00:00.660 - 00:00:13.080] text",
                   no speakers at all
  .docx named      "Name: text" lines, or plain text with no speakers
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
# "Ivan Petrov: text" (colon after a short name)
RE_NAMED = re.compile(r"^([^:]{2,60}?):\s+(.+)$", re.S)
# "Ivan Petrov<tab>12:34" — the speaker heading in a current Teams .docx. The
# separator may be a tab, several spaces or a single one; the time may carry AM/PM.
RE_HEADING = re.compile(r"^(.{2,60}?)[\s ]+\d{1,2}:\d{2}(:\d{2})?(\s*[AP]M)?$", re.I)
# "[00:00:00.660 - 00:00:13.080] text"
STAMP = r"(?:\d{1,2}:)?\d{1,2}:\d{2}(?:[.,]\d+)?"
RE_SEGMENT = re.compile(rf"^\[\s*{STAMP}\s*[-–—]\s*{STAMP}\s*\]\s*(.*)$")
# "Ivan Petrov started transcription" — a line Teams writes, not one anybody said
RE_TRANSCRIPTION = re.compile(r"^.{2,60} (started|stopped) transcription$", re.I)
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


# ---------------------------------------------------------------- .vtt

def _vtt_body(path: Path) -> list:
    """The cue text lines of a .vtt, without the header, timecodes and cue ids."""
    lines = Path(path).read_text(encoding="utf-8-sig", errors="replace").splitlines()
    body, started = [], False
    for i, raw in enumerate(lines):
        s = raw.strip()
        # Everything before the first timecode is the file header: WEBVTT,
        # X-TIMESTAMP-MAP, NOTE blocks and so on.
        if RE_TIMECODE.match(s):
            started = True
            continue
        if not started or not s:
            continue
        # The line right before a timecode is a cue identifier, whatever its shape.
        if i + 1 < len(lines) and RE_TIMECODE.match(lines[i + 1].strip()):
            continue
        if s.split()[0].upper() in SERVICE_WORDS or s.isdigit() or RE_CUE_GUID.fullmatch(s):
            continue
        body.append(s)
    return body


def parse_vtt(path: Path) -> Parsed:
    body = _vtt_body(path)
    voiced = any(RE_VOICE.match(line) for line in body)
    turns, current = [], UNKNOWN
    for line in body:
        m = RE_VOICE.match(line) if voiced else None
        if not voiced:
            m = RE_NAMED.match(line)
            if m and line.startswith("http"):
                m = None
        if m:
            current = _clean(m.group(1))
            turns.append([current, _clean(m.group(2))])
        elif turns:                         # the rest of the turn on the next line
            turns[-1][1] = f"{turns[-1][1]} {_clean(line)}".strip()
        else:
            turns.append([current, _clean(line)])
    return Parsed(_merge(turns))


# ---------------------------------------------------------------- .docx

def _docx_lines(path: Path) -> list:
    """Every non-empty line, a paragraph's soft line breaks included."""
    from docx import Document

    lines = []
    for para in Document(str(path)).paragraphs:
        lines += [s.strip() for s in para.text.split("\n") if s.strip()]
    return lines


def _heading(line: str):
    """The speaker of a "Name   0:03" heading, or None."""
    m = RE_HEADING.match(line)
    return _clean(m.group(1)) if m and _looks_like_name(m.group(1)) else None


def _docx_shape(lines: list) -> str:
    def count(test):
        return sum(1 for line in lines if test(line))

    if count(RE_SEGMENT.match) >= 2:
        return "segments"
    if count(RE_TIMECODE.match) >= 2:
        return "timecode"
    if count(_heading) >= 2:
        return "heading"
    return "named"


def _docx_segments(lines):
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
    return Parsed([t for t in turns if t[1]], named=False)


def _docx_timecode(lines):
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
    return Parsed(_merge(turns))


def _docx_heading(lines):
    turns, speaker = [], None
    for line in lines:
        if RE_TRANSCRIPTION.match(line):
            continue
        name = _heading(line)
        if name:
            speaker = name
        elif speaker:                       # before the first heading: the header
            turns.append([speaker, _clean(line)])
    return Parsed(_merge(turns))


def _docx_named(lines):
    turns, speaker, header = [], None, []
    for line in lines:
        m = RE_NAMED.match(line)
        if m:
            speaker = _clean(m.group(1))
            turns.append([speaker, _clean(m.group(2))])
        elif speaker:
            turns.append([speaker, _clean(line)])
        else:
            header.append(_clean(line))     # the title and date, before any speaker
    # No speaker found at all — hand back the text rather than lose it.
    if not turns and header:
        turns = [[UNKNOWN, " ".join(header)]]
    return Parsed(_merge(turns))


DOCX_SHAPES = {"segments": _docx_segments, "timecode": _docx_timecode,
               "heading": _docx_heading, "named": _docx_named}


def parse_docx(path: Path) -> Parsed:
    lines = _docx_lines(path)
    return DOCX_SHAPES[_docx_shape(lines)](lines)


# ---------------------------------------------------------------- the rest

def parse(path) -> Parsed:
    path = Path(path)
    ext = path.suffix.lower()
    if ext == ".vtt":
        return parse_vtt(path)
    if ext == ".docx":
        return parse_docx(path)
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
    long enough. Anything else has to pass three signals, in order of how much
    they discriminate:
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
