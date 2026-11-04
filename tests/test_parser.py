"""Checks for the shapes Teams, Zoom, Google Meet, Otter and others hand out a
transcript in.

Run: .venv/bin/python -m pytest -q

The fixtures reconstruct real exports (Teams: Recap tab -> Download); the
other apps' shapes are reconstructions too, not files those apps wrote. Transcript
text stays Russian on purpose: that is the language these meetings are in, and
the parser has to survive it. One dialogue is rendered into every dialect, so
the dialects are compared against the same expected result.
"""
from pathlib import Path

import pytest

import parser as tparser

A, B = "Ярослав Ветерба", "Мария Соколова"
DIALOGUE = [
    (A, "Коллеги, начнём. Первое, что нужно закрыть сегодня, это дата релиза."),
    (B, "По документации я успеваю к пятнице, раздел про историю платежей почти готов."),
    (A, "Хорошо. Тогда запускаем пилот в понедельник, тридцатого сентября."),
    (B, "Принято. Я предупрежу поддержку до четверга и пришлю им инструкцию."),
    (A, "Отдельно нужен шаблон письма клиентам, он тоже на тебе, срок пятница."),
    (B, "Сделаю оба пункта и отпишусь в общем чате, как только будет готово."),
    (A, "Остался открытый вопрос про старый платёжный шлюз, но это не сегодня."),
    (B, "Согласна, вернёмся к нему, когда юристы ответят по договору."),
]
CUE = "0d5a1b9a-3c4e-4e5f-8a1b-2c3d4e5f6a7b"

# Junk from the container that must never reach the model.
JUNK = ("-->", "WEBVTT", "X-TIMESTAMP-MAP", "</v>", "00:00:", "0:0:", "transcription")


def timecode(i):
    a, b = 2 + i * 6, 7 + i * 6
    return f"00:{a // 60:02d}:{a % 60:02d}.000 --> 00:{b // 60:02d}:{b % 60:02d}.000"


def old_timecode(i):
    return f"0:0:{i * 6}.0 --> 0:0:{i * 6 + 5}.370"


def build_vtt(tmp, name, turns, dialect="classic", header="WEBVTT\n") -> Path:
    blocks = [header]
    for i, (speaker, text) in enumerate(turns):
        if dialect == "classic":
            cue_id, body = f"{CUE}/{28 + i}-0", f"<v {speaker}>{text}</v>"
        elif dialect == "open":           # some tenants omit the closing tag
            cue_id, body = str(i + 1), f"<v {speaker}>{text}"
        elif dialect == "flat":           # no tags at all
            cue_id, body = str(i + 1), f"{speaker}: {text}"
        elif dialect == "old":            # old Teams: short timecodes, no cue ids
            cue_id, body = "", f"<v {speaker}>{text}</v>"
        else:
            raise ValueError(dialect)
        code = old_timecode(i) if dialect == "old" else timecode(i)
        blocks.append(f"\n{cue_id}\n{code}\n{body}\n" if cue_id else f"\n{code}\n{body}\n")
    p = tmp / f"{name}.vtt"
    p.write_text("".join(blocks), encoding="utf-8")
    return p


def build_docx(tmp, name, paragraphs) -> Path:
    from docx import Document
    doc = Document()
    for line in paragraphs:
        doc.add_paragraph(line)
    p = tmp / f"{name}.docx"
    doc.save(str(p))
    return p


def build_text(tmp, name, lines, ext=".txt", encoding="utf-8") -> Path:
    p = tmp / f"{name}{ext}"
    p.write_text("\n".join(lines) + "\n", encoding=encoding)
    return p


def build_srt(tmp, name, texts) -> Path:
    blocks = [f"{i + 1}\n00:00:{i * 6:02d},000 --> 00:00:{i * 6 + 5:02d},000\n{t}\n"
              for i, t in enumerate(texts)]
    p = tmp / f"{name}.srt"
    p.write_text("\n".join(blocks), encoding="utf-8")
    return p


# ---------------------------------------------------------------- accepted

# One long turn split across several consecutive cues has to be rejoined.
SPLIT = [(A, "Мне нужно до четверга"), (A, "подготовить выборку из десяти встреч.")]
# A guest keeps the suffix Teams gives them.
GUEST = [(f"{A} (Guest)" if s == A else s, t) for s, t in DIALOGUE]

DOCX_TAB = sum(
    [["Совещание по проекту", "23 сентября 2026 г.", ""]] +
    [[f"{s}\t0:{i * 7:02d}", t, ""] for i, (s, t) in enumerate(DIALOGUE)], [])
DOCX_SPACES = sum(
    [[f"{s}   0:00:{i * 7:02d}", t] for i, (s, t) in enumerate(DIALOGUE)], [])

# Current Teams .docx: a header whose date ends in a time, a line Teams writes
# itself, and speech that has a colon or a clock time in it.
DOCX_CURRENT = (
    ["Совещание по проекту", "October 6, 2026, 10:00AM", "32m 14s",
     f"{A} started transcription"]
    + sum([[f"{s}   {i // 60}:{i % 60:02d}", t] for i, (s, t) in enumerate(DIALOGUE)], [])
    [:6]
    + ["План на сегодня: закрыть релиз и шлюз.", "Созвонимся завтра в 10:30"]
    + sum([[f"{s}   1:{i:02d}", t] for i, (s, t) in enumerate(DIALOGUE[3:], 3)], [])
    + [f"{A} stopped transcription"])
# Old Teams .docx: the timecode, the name and the text in one paragraph.
OLD_SPEAKERS = {A: "Speaker 1", B: "Speaker 2"}
DOCX_OLD = [f"{old_timecode(i)}\n{OLD_SPEAKERS[s]}\n{t}" for i, (s, t) in enumerate(DIALOGUE)]
OLD = [(OLD_SPEAKERS[s], t) for s, t in DIALOGUE]

# Otter: "Name  0:03", the text below, a blank line.
OTTER = sum([[f"{s}  0:{i * 7:02d}", t, ""] for i, (s, t) in enumerate(DIALOGUE)],
            ["Совещание по проекту", "Tue, Sep 23, 2026 10:02AM • 12:44", ""])
# Zoom's saved transcript: "[Name] 10:15:32", the text below.
ZOOM_TXT = sum([[f"[{s}] 10:15:{i * 7:02d}", t] for i, (s, t) in enumerate(DIALOGUE)], [])
# Google Meet's transcript document: a header, a bare time every few minutes, the
# end of the meeting.
MEET = (["Совещание по проекту - Transcript", "Attendees", f"{A}, {B}", "Transcript",
         "00:00:00"]
        + [f"{s}: {t}" for s, t in DIALOGUE[:4]] + ["00:05:00"]
        + [f"{s}: {t}" for s, t in DIALOGUE[4:]] + ["Meeting ended after 00:07:12 👋"])
STAMPED_FRONT = [f"[00:0{i}:{i * 7:02d}] {s}: {t}" for i, (s, t) in enumerate(DIALOGUE)]
STAMPED_AFTER = [f"{s} (00:0{i}:{i * 7:02d}): {t}" for i, (s, t) in enumerate(DIALOGUE)]
DASHED = sum([[f"{s} - 0{i}:{i * 7:02d}", t] for i, (s, t) in enumerate(DIALOGUE)], [])
# Markdown, as Granola or a notes app copies it out.
MARKDOWN = ["# Совещание по проекту", ""] + [f"**{s}:** {t}" for s, t in DIALOGUE]

ACCEPTED = [
    pytest.param(
        lambda d: build_vtt(d, "classic", DIALOGUE, "classic"), [A, B],
        ["Коллеги, начнём.", "вернёмся к нему, когда юристы ответят"],
        id="vtt-classic-teams"),
    pytest.param(
        lambda d: build_vtt(d, "open", DIALOGUE, "open"), [A, B],
        ["Коллеги, начнём."], id="vtt-no-closing-tag"),
    pytest.param(
        lambda d: build_vtt(d, "flat", DIALOGUE, "flat"), [A, B],
        ["Коллеги, начнём."], id="vtt-flat-name-colon-text"),
    pytest.param(
        lambda d: build_vtt(
            d, "header", DIALOGUE, "classic",
            header="WEBVTT\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:0\n\n"
                   "NOTE\nTranscript generated by Microsoft Teams\n"),
        [A, B], ["Коллеги, начнём."], id="vtt-metadata-header"),
    pytest.param(
        lambda d: build_vtt(d, "split", SPLIT + DIALOGUE[1:], "classic"), [A, B],
        ["Мне нужно до четверга. подготовить выборку из десяти встреч."],
        id="vtt-split-turn-rejoined"),
    pytest.param(
        lambda d: build_vtt(d, "guest", GUEST, "classic"), [f"{A} (Guest)", B], [],
        id="vtt-guest-suffix"),
    pytest.param(
        lambda d: build_docx(d, "tab", DOCX_TAB), [A, B], ["Коллеги, начнём."],
        id="docx-name-and-time-via-tab"),
    pytest.param(
        lambda d: build_docx(d, "spaces", DOCX_SPACES), [A, B], ["Коллеги, начнём."],
        id="docx-name-and-time-via-spaces"),
    pytest.param(
        lambda d: build_vtt(d, "old", OLD, "old"), ["Speaker 1", "Speaker 2"],
        ["Коллеги, начнём."], id="vtt-old-teams-short-timecodes"),
    pytest.param(
        lambda d: build_docx(d, "old", DOCX_OLD), ["Speaker 1", "Speaker 2"],
        ["Коллеги, начнём.", "юристы ответят по договору"], id="docx-old-teams-one-paragraph"),
    pytest.param(
        lambda d: build_docx(d, "current", DOCX_CURRENT), [A, B],
        ["План на сегодня: закрыть релиз", "Созвонимся завтра в 10:30"],
        id="docx-current-teams-header-and-colons"),
    pytest.param(
        lambda d: build_srt(d, "zoom", [f"{s}: {t}" for s, t in DIALOGUE]), [A, B],
        ["Коллеги, начнём."], id="srt-name-colon-text"),
    pytest.param(
        lambda d: build_text(d, "otter", OTTER), [A, B], ["Коллеги, начнём."],
        id="txt-otter-name-and-time"),
    pytest.param(
        lambda d: build_text(d, "zoom", ZOOM_TXT), [A, B], ["Коллеги, начнём."],
        id="txt-zoom-bracketed-name"),
    pytest.param(
        lambda d: build_docx(d, "meet", MEET), [A, B],
        ["Коллеги, начнём.", "юристы ответят по договору"], id="docx-google-meet"),
    pytest.param(
        lambda d: build_text(d, "meet", MEET), [A, B], ["Коллеги, начнём."],
        id="txt-google-meet"),
    pytest.param(
        lambda d: build_text(d, "front", STAMPED_FRONT), [A, B], ["Коллеги, начнём."],
        id="txt-time-before-the-name"),
    pytest.param(
        lambda d: build_text(d, "after", STAMPED_AFTER), [A, B], ["Коллеги, начнём."],
        id="txt-time-after-the-name"),
    pytest.param(
        lambda d: build_docx(d, "dashed", DASHED), [A, B], ["Коллеги, начнём."],
        id="docx-name-dash-time"),
    pytest.param(
        lambda d: build_text(d, "granola", MARKDOWN, ".md"), [A, B], ["Коллеги, начнём."],
        id="md-bold-names"),
    pytest.param(
        lambda d: build_text(d, "windows", [f"{s}: {t}" for s, t in DIALOGUE],
                             encoding="utf-16"), [A, B], ["Коллеги, начнём."],
        id="txt-utf16-from-windows"),
    pytest.param(
        lambda d: build_text(d, "vtt-as-txt", build_vtt(d, "v", DIALOGUE).read_text()
                             .splitlines()), [A, B], ["Коллеги, начнём."],
        id="txt-holding-a-vtt"),
]


@pytest.mark.parametrize("build, want_speakers, want_fragments", ACCEPTED)
def test_accepted(tmp_path, build, want_speakers, want_fragments):
    """Must parse correctly AND be recognised as a transcript."""
    parsed = tparser.parse(build(tmp_path))
    turns = parsed.turns
    text = tparser.to_text(turns)

    assert tparser.speakers(turns) == want_speakers
    for frag in want_fragments:
        assert frag in text, f"lost fragment {frag!r}"
    for junk in JUNK + ("Meeting ended", "Attendees", "**"):
        assert junk not in text, f"junk leaked into the text: {junk!r}"
    ok, reason = tparser.looks_like_transcript(parsed)
    assert ok, f"rejected as not a transcript: {reason}"


# ---------------------------------------------------------------- rejected

MOVIE = [
    ("", "It was a bright cold day in April, and the clocks were striking thirteen."),
    ("", "Winston Smith slipped quickly through the glass doors of Victory Mansions."),
    ("", "The hallway smelt of boiled cabbage and old rag mats all the way up."),
    ("", "At one end of it a coloured poster had been tacked to the wall."),
    ("", "It depicted simply an enormous face, more than a metre wide."),
    ("", "The face of a man of about forty-five, with a heavy black moustache."),
]

# An ordinary document: headings end in a colon, so the colon rule picks them up
# as "speakers" — but each occurs exactly once, which is the giveaway.
REPORT = [
    "Project report",
    "Period covered: 12-21 September 2026, version 0.9.3 at the close of the period.",
    "One rule underlies most of what follows: the notes are plain files on disk.",
    "Both halves were verified: typing survived the application's own save cycle.",
    "The index runs separately: a crash there never takes the editor down with it.",
    "Planned next: a proper migration path and a second pass over the search index.",
    "Known gaps: no conflict resolution yet, and the importer skips attachments.",
    "Open risks: the vendor contract is unsigned, which blocks the staging setup.",
]

AGENDA = [
    "Agenda for the weekly product sync",
    "Goal: agree the release date and close out the payment gateway question.",
    "Owner: the product team, with engineering joining for the second half hour.",
    "Duration: forty five minutes, a hard stop because of the following meeting.",
    "Materials: the pilot metrics dashboard and the draft migration plan document.",
    "Prework: everyone reads the incident write-up from last Tuesday beforehand.",
    "Outcome: a dated plan that engineering has actually committed to deliver.",
]

PROSE = [
    "Главная страница",
    "Bilpleie med stolthet. Vi kommer til deg, uansett hvor du er i byen.",
    "Bestill time på nett, så tar vi kontakt innen en virkedag for å bekrefte.",
    "Vi bruker kun produkter som er trygge for lakken og for miljøet rundt oss.",
]

REJECTED = [
    pytest.param(lambda d: build_vtt(d, "movie", MOVIE, "flat"), "no speaker names",
                 id="subtitles-feature-film"),
    pytest.param(lambda d: build_srt(d, "movie", [t for _, t in MOVIE]), "no speaker names",
                 id="srt-feature-film"),
    pytest.param(lambda d: build_text(d, "agenda", AGENDA, ".md"), "nobody speaks twice",
                 id="md-meeting-agenda"),
    pytest.param(lambda d: build_docx(d, "report", REPORT), "nobody speaks twice",
                 id="docx-ordinary-document"),
    pytest.param(lambda d: build_docx(d, "agenda", AGENDA), "nobody speaks twice",
                 id="docx-meeting-agenda"),
    pytest.param(lambda d: build_docx(d, "prose", PROSE), "no speaker names",
                 id="docx-plain-prose-no-structure"),
]


@pytest.mark.parametrize("build, expect_in_reason", REJECTED)
def test_rejected(tmp_path, build, expect_in_reason):
    """Must NOT be handed to the model, and for the stated reason."""
    try:
        parsed = tparser.parse(build(tmp_path))
    except Exception:
        return          # never reached the model either, which is the point
    ok, reason = tparser.looks_like_transcript(parsed)
    assert not ok, "accepted as a transcript, should have been rejected"
    assert expect_in_reason in reason, f"rejected for the wrong reason — {reason}"


# ---------------------------------------------------------------- no speakers

SEGMENTS = (["Transkript razgovora", "Datoteka: sastanak.m4a"]
            + [f"[00:00:{i * 7:02d}.660 - 00:00:{i * 7 + 6:02d}.080] {t}"
               for i, (_, t) in enumerate(DIALOGUE)])


def test_a_speech_to_text_export_is_one_turn_per_segment_and_names_nobody(tmp_path):
    parsed = tparser.parse(build_docx(tmp_path, "segments", SEGMENTS))

    assert parsed.named is False
    assert [t for _, t in parsed.turns] == [t for _, t in DIALOGUE]
    assert tparser.speakers(parsed.turns) == [tparser.UNKNOWN]
    assert tparser.looks_like_transcript(parsed) == (True, "")
    # The model reads the words alone, not "Unknown: …" on every line.
    assert tparser.to_text(parsed.turns).splitlines()[0] == DIALOGUE[0][1]


def test_subtitles_without_names_are_one_turn_per_cue(tmp_path):
    parsed = tparser.parse(build_srt(tmp_path, "whisper", [t for _, t in DIALOGUE]))

    assert (parsed.named, parsed.shape) == (False, "subtitles")
    assert [t for _, t in parsed.turns] == [t for _, t in DIALOGUE]
    # Nobody chose it, so it could be a film: the watcher leaves it alone.
    assert not tparser.looks_like_transcript(parsed)[0]


def test_a_whisper_text_export_is_read_as_segments(tmp_path):
    lines = [f"[00:{i * 7:02d}.000 --> 00:{i * 7 + 6:02d}.000]  {t}"
             for i, (_, t) in enumerate(DIALOGUE)]
    parsed = tparser.parse(build_text(tmp_path, "whisper", lines))

    assert (parsed.named, parsed.shape) == (False, "segments")
    assert [t for _, t in parsed.turns] == [t for _, t in DIALOGUE]


def test_the_same_meeting_from_another_app_is_the_same_meeting(tmp_path):
    from history import fingerprint
    shapes = [build_vtt(tmp_path, "teams", DIALOGUE), build_text(tmp_path, "meet", MEET),
              build_text(tmp_path, "otter", OTTER), build_text(tmp_path, "md", MARKDOWN, ".md")]
    assert len({fingerprint(tparser.parse(p).turns) for p in shapes}) == 1


def test_files_in_a_folder_leave_out_hidden_lock_and_other_files(tmp_path):
    for name in ["a.vtt", "sub/b.docx", "sub/c.SRT", "d.md", "notes.pdf", ".e.txt",
                 "~$f.docx", ".hidden/g.vtt"]:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("x")
    found = [str(p.relative_to(tmp_path)) for p in tparser.files_in(tmp_path)]
    assert found == ["a.vtt", "d.md", "sub/b.docx", "sub/c.SRT"]


def test_the_current_docx_header_never_becomes_a_speaker(tmp_path):
    parsed = tparser.parse(build_docx(tmp_path, "current", DOCX_CURRENT))
    text = tparser.to_text(parsed.turns)

    assert "October" not in text and "32m" not in text
    assert "Совещание по проекту" not in text
    assert all(s in (A, B) for s in tparser.speakers(parsed.turns))


@pytest.mark.parametrize("name, ok", [
    ("Ivan Petrov", True), ("Alexey Gromov (Guest)", True), ("Speaker 2", True),
    ("Ludwig van Beethoven", True), ("Ярослав Ветерба", True),
    ("October 6, 2026,", False), ("Let's meet at", False), ("Созвонимся завтра в", False),
    ("Both halves were verified", False), ("2026 report", False),
])
def test_what_counts_as_a_name(name, ok):
    assert tparser._looks_like_name(name) is ok
