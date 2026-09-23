"""Parse Teams transcripts (.vtt / .docx) into speaker turns."""
import os
import re
from pathlib import Path

UNKNOWN = "Unknown"

# Minimum shape a file must have before we spend minutes of LLM time on it.
MIN_TURNS = int(os.getenv("MIN_TURNS", "4"))
MIN_CHARS = int(os.getenv("MIN_CHARS", "400"))
# People take turns, so a speaker recurs. Section headings in a document do not.
MIN_TURNS_PER_SPEAKER = float(os.getenv("MIN_TURNS_PER_SPEAKER", "2.0"))
# How many of the detected speakers must actually be shaped like a person's name.
MIN_NAME_RATIO = float(os.getenv("MIN_NAME_RATIO", "0.6"))

# "<v Ivan Petrov>text</v>" — the Teams VTT voice tag
RE_VOICE = re.compile(r"<v\s+([^>]+?)\s*>(.*?)(?:</v>)?\s*$", re.S)
# "00:00:01.234 --> 00:00:05.678"
RE_TIMECODE = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?[.,]\d{1,3}\s*-->")
# "Ivan Petrov: text" (colon after a short name)
RE_NAMED = re.compile(r"^([^:]{2,60}?):\s+(.+)$", re.S)
# "Ivan Petrov<tab>12:34" — the speaker heading in a .docx export. The separator
# may be a tab, several spaces or a single one; the time may carry an AM/PM suffix.
RE_SPEAKER_LINE = re.compile(
    r"^(.{2,60}?)[\s ]+\d{1,2}:\d{2}(:\d{2})?(\s*[AP]M)?$", re.I)
RE_TAGS = re.compile(r"<[^>]+>")

SERVICE_LINES = {"WEBVTT", "NOTE", "STYLE", "REGION"}


def _is_service(line: str) -> bool:
    s = line.strip()
    if not s:
        return True
    if s.split()[0].upper() in SERVICE_LINES:
        return True
    if RE_TIMECODE.match(s):
        return True
    if s.isdigit():  # cue sequence number
        return True
    if re.fullmatch(r"[0-9a-fA-F\-]{8,}(/\d+-\d+)?", s):  # Teams cue GUID
        return True
    return False


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


def parse_vtt(path: Path):
    turns = []
    current = None
    lines = Path(path).read_text(encoding="utf-8-sig", errors="replace").splitlines()
    started = False
    for i, raw in enumerate(lines):
        # Everything before the first timecode is the file header:
        # WEBVTT, X-TIMESTAMP-MAP, NOTE blocks and so on.
        if not started:
            if RE_TIMECODE.match(raw.strip()):
                started = True
            continue
        # The line right before a timecode is a cue identifier, whatever its shape.
        if i + 1 < len(lines) and RE_TIMECODE.match(lines[i + 1].strip()):
            continue
        if _is_service(raw):
            continue
        line = raw.strip()
        m = RE_VOICE.match(line)
        if m:
            current = _clean(m.group(1))
            turns.append([current, _clean(m.group(2))])
            continue
        m = RE_NAMED.match(line)
        if m and not line.startswith("http"):
            current = _clean(m.group(1))
            turns.append([current, _clean(m.group(2))])
            continue
        # Continuation of the previous turn.
        if turns:
            turns[-1][1] = (turns[-1][1] + " " + _clean(line)).strip()
        else:
            turns.append([current or UNKNOWN, _clean(line)])
    return _merge(turns)


def parse_docx(path: Path):
    from docx import Document

    turns = []
    speaker = None
    header = []          # meeting title and date, before the first speaker
    for para in Document(str(path)).paragraphs:
        line = para.text.strip()
        if _is_service(line):
            continue
        m = RE_SPEAKER_LINE.match(line)
        if m:
            speaker = _clean(m.group(1))
            continue
        m = RE_NAMED.match(line)
        if m:
            speaker = _clean(m.group(1))
            turns.append([speaker, _clean(m.group(2))])
            continue
        if speaker is None:
            header.append(_clean(line))   # collect the header, then drop it
            continue
        turns.append([speaker, _clean(line)])

    # No speaker found at all — hand back the text rather than lose it.
    if not turns and header:
        turns = [[UNKNOWN, " ".join(header)]]
    return _merge(turns)


def parse(path) -> list:
    """Return [(speaker, text), ...]."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext == ".vtt":
        return parse_vtt(path)
    if ext == ".docx":
        return parse_docx(path)
    raise ValueError(f"Unsupported format: {ext}")


def to_text(turns) -> str:
    return "\n".join(f"{s}: {t}" for s, t in turns)


def speakers(turns) -> list:
    seen = []
    for s, _ in turns:
        if s not in seen:
            seen.append(s)
    return seen


def _looks_like_name(s: str) -> bool:
    """A person's name is short and has no sentence in it.

    "Ivan Petrov", "Alexey Gromov (Guest)" pass; "Both halves were verified in
    the running application" — a heading the colon rule picked up — does not.
    """
    s = s.strip()
    if not s or len(s) > 48 or not any(c.isalpha() for c in s):
        return False
    if any(c in s for c in ".!?;"):
        return False
    if any(c.isdigit() for c in s):
        return False
    return 1 <= len(s.split()) <= 4


def looks_like_transcript(turns) -> tuple:
    """Is this a meeting transcript, or just some .vtt/.docx that landed nearby?

    Returns (ok, reason). Cheap: runs on the already parsed turns, no LLM call.

    Three signals, in order of how much they discriminate:
      1. Somebody is speaking at all. A movie subtitle file carries no speaker
         tags and an ordinary document has no speaker headings, so both collapse
         into one Unknown turn.
      2. Speakers recur. A conversation alternates between a few people; a
         document gives every "speaker" exactly one turn, because what the colon
         rule matched was a section heading, not a name.
      3. The names are shaped like names, not like sentences.
    """
    if not turns:
        return False, "no dialogue lines found"

    distinct = [s for s in speakers(turns) if s != UNKNOWN]
    if not distinct:
        return False, "no speaker names found"

    if len(turns) < MIN_TURNS:
        return False, f"only {len(turns)} turns, need at least {MIN_TURNS}"

    size = len(to_text(turns))
    if size < MIN_CHARS:
        return False, f"only {size} characters, need at least {MIN_CHARS}"

    per_speaker = len(turns) / len(distinct)
    if per_speaker < MIN_TURNS_PER_SPEAKER:
        return False, (f"{len(distinct)} speakers for {len(turns)} turns — nobody "
                       f"speaks twice, so this reads as a document, not a conversation")

    name_ratio = sum(_looks_like_name(s) for s in distinct) / len(distinct)
    if name_ratio < MIN_NAME_RATIO:
        return False, (f"only {name_ratio:.0%} of the detected speakers look like "
                       f"names, the rest look like headings")

    return True, ""
