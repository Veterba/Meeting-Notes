"""Who said what on the other side of a call.

Everyone else in a call arrives mixed into one stream, system.wav. After the
call, sherpa-onnx tells their voices apart (speaker diarization: pyannote's
segmentation model finds the turns, a speaker-embedding model groups them by
voice), and each of their lines is relabelled Speaker 1, Speaker 2… by which
voice was speaking when it was said. One voice stays "Them".

When the call is in Teams, the recorder also notes whose name Teams marks as
speaking (speaking.json in the recording's folder). Those names replace the
labels: a line gets the name shown while it was said, or else the one its voice
had most often. A name shown mostly while I talk is mine, and is left out.

The names come later, from the user: renaming "Speaker 1" to "Anna" rewrites the
meeting's notes in place and is remembered for its transcript, so the model
never has to run again for it.
"""
import json
import logging
import re
import tarfile
from array import array
from collections import Counter, defaultdict
from dataclasses import replace
from pathlib import Path

import paths
from transcriber import WavTail, download

log = logging.getLogger("pipeline")

RELEASES = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
SEGMENTATION_URL = (f"{RELEASES}/speaker-segmentation-models/"
                    "sherpa-onnx-pyannote-segmentation-3-0.tar.bz2")
EMBEDDING_NAME = "nemo_en_titanet_small.onnx"
EMBEDDING_URL = f"{RELEASES}/speaker-recongition-models/{EMBEDDING_NAME}"   # sic, their tag
# How alike two voices must be to count as one person. On synthetic calls of one,
# two and three voices any value from 0.3 to 0.8 told them apart; a real call's one
# voice, through a call app's compression, only stays one from 0.7 up.
THRESHOLD = 0.7
# Seconds. A voice that says less than this in the whole call is a stray piece of
# someone else's (a cough, a word through bad audio), not another person.
SHORTEST_VOICE = 10
# What Teams' window says beside the person talking, as the accessibility API reads
# it. The recorder picks labels with it, so it must suit both Python and ICU:
# group 1 is the name.
SPEAKING = r"(?i)^(.+?)[,;:]?\s+(?:is\s+)?(?:speaking|talking|говорит|snakker)\b"
SPEAKING_FILE = "speaking.json"
NAMES = "_speakers"             # in a meeting's notes: {"Speaker 1": "Anna", ...}


def models_dir() -> Path:
    return paths.DATA / "models" / "speakers"


def segmentation_model() -> Path:
    return models_dir() / "sherpa-onnx-pyannote-segmentation-3-0" / "model.int8.onnx"


def embedding_model() -> Path:
    return models_dir() / EMBEDDING_NAME


def ensure_models(status=lambda text: None):
    """Download the two small models (about 45 MB) the first time."""
    if not segmentation_model().exists():
        status("downloading the speaker models")
        archive = models_dir() / "segmentation.tar.bz2"
        download(SEGMENTATION_URL, archive)
        with tarfile.open(archive) as tar:
            tar.extractall(models_dir(), filter="data")
        archive.unlink()
    if not embedding_model().exists():
        status("downloading the speaker models")
        download(EMBEDDING_URL, embedding_model())


def diarize(wav: Path) -> list:
    """[(voice number, start s, end s)] for a 16 kHz mono recording, in time order."""
    import sherpa_onnx as so    # loaded only when a call is being sorted out

    samples = WavTail(wav).read()
    if not samples:
        return []
    config = so.OfflineSpeakerDiarizationConfig(
        segmentation=so.OfflineSpeakerSegmentationModelConfig(
            pyannote=so.OfflineSpeakerSegmentationPyannoteModelConfig(
                model=str(segmentation_model()))),
        embedding=so.SpeakerEmbeddingExtractorConfig(model=str(embedding_model())),
        clustering=so.FastClusteringConfig(num_clusters=-1, threshold=THRESHOLD),
        min_duration_on=0.3, min_duration_off=0.5)
    floats = array("f", (s / 32768 for s in samples))
    result = so.OfflineSpeakerDiarization(config).process(floats).sort_by_start_time()
    return [(r.speaker, r.start, r.end) for r in result]


def assign(lines: list, turns: list) -> list:
    """Their lines relabelled by the voice that overlaps each the most, or the
    nearest one when none does.

    Voices are numbered by when they first speak, so Speaker 1 opens the call.
    A voice heard too little is left out, and its lines go to a real one. Mine
    are untouched.
    """
    talk = Counter()
    for who, start, end in turns:
        talk[who] += end - start
    turns = [t for t in turns if talk[t[0]] >= SHORTEST_VOICE]
    if not turns:
        return lines

    def voice(line):
        # A gap counts as a negative overlap, so the nearest voice wins.
        return max(turns, key=lambda t: min(t[2], line.end) - max(t[1], line.start))[0]

    voices = [voice(line) if line.who == "them" else None for line in lines]
    if len(set(voices) - {None}) < 2:
        return lines                    # one voice on their side: "Them" says it
    number, out = {}, []
    for line, v in zip(lines, voices):
        if v is not None:
            number.setdefault(v, len(number) + 1)
            line = replace(line, who=f"Speaker {number[v]}")
        out.append(line)
    return out


def timeline(folder: Path) -> list:
    """[(name, start s, end s)]: when the call app showed each name as speaking."""
    path = Path(folder) / SPEAKING_FILE
    if not path.exists():
        return []
    pattern = re.compile(SPEAKING)
    spans, since, t = [], {}, 0.0
    for t, labels in json.loads(path.read_text(encoding="utf-8")):
        now = {m[1].strip() for label in labels if (m := pattern.search(label))}
        for name in now - since.keys():
            since[name] = t
        for name in since.keys() - now:
            spans.append((name, since.pop(name), t))
    spans += [(name, start, t) for name, start in since.items()]
    return sorted(spans, key=lambda span: span[1])


def put_names(lines: list, spans: list) -> list:
    """Their lines named by the call app: the name shown most while a line was
    said, or else the one shown most while its voice spoke."""
    if not spans:
        return lines
    shown = defaultdict(Counter)            # a line -> name -> seconds shown during it
    mine, theirs = Counter(), Counter()
    for i, line in enumerate(lines):
        for name, start, end in spans:
            overlap = min(end, line.end) - max(start, line.start)
            if overlap > 0:
                shown[i][name] += overlap
                (mine if line.who == "me" else theirs)[name] += overlap
    # Teams marks me speaking too; a name heard mostly over my lines is mine.
    own = {name for name in mine if mine[name] >= theirs[name]}
    voice = defaultdict(Counter)
    for i, line in enumerate(lines):
        if line.who != "me":
            voice[line.who].update(shown[i])

    def best(counts):
        counts = {name: s for name, s in counts.items() if name not in own}
        return max(counts, key=counts.get) if counts else None

    out = []
    for i, line in enumerate(lines):
        name = None if line.who == "me" else best(shown[i]) or best(voice[line.who])
        out.append(replace(line, who=name) if name else line)
    return out


def relabel(lines: list, folder: Path, status=lambda text: None) -> list:
    """Live's last step: their lines by voice, then by the names the call app
    showed. On any failure the lines stay as they were — a call with one "Them"
    is still worth its notes."""
    if not any(line.who == "them" for line in lines):
        return lines
    try:
        ensure_models(status)
        status("telling the speakers apart")
        lines = assign(lines, diarize(Path(folder) / "system.wav"))
    except Exception as e:
        log.error("Could not tell the speakers apart in %s: %s", Path(folder).name, e)
    try:
        return put_names(lines, timeline(folder))
    except Exception as e:
        log.error("Could not read who was speaking in %s: %s", Path(folder).name, e)
        return lines


# ---------------------------------------------------------------- naming them

def rename(notes: dict, old: str, new: str) -> dict:
    """Give a speaker a name, in every part of the notes, and remember it.

    The label is replaced as a whole word, so Speaker 1 never touches Speaker 10.
    """
    new = new.strip()
    if not new or not old:
        raise ValueError("a speaker needs a name")
    names = dict(notes.get(NAMES) or {})
    # Renaming a renamed speaker again: the original label is what the
    # transcript says, so that is what the map keeps.
    label = next((k for k, v in names.items() if v == old), old)
    names[label] = new
    pattern = re.compile(rf"(?<!\w){re.escape(old)}(?!\w)")

    def swap(value):
        if isinstance(value, str):
            return pattern.sub(new, value)
        if isinstance(value, list):
            return [swap(v) for v in value]
        if isinstance(value, dict):
            return {k: (v if k.startswith("_") else swap(v)) for k, v in value.items()}
        return value

    out = swap({k: v for k, v in notes.items()})
    out[NAMES] = names
    return out


def named(turns: list, notes: dict) -> list:
    """A transcript's turns with the names given so far."""
    names = (notes or {}).get(NAMES) or {}
    return [(names.get(speaker, speaker), text) for speaker, text in turns]
