"""Who said what on the other side of a call.

Everyone else in a call arrives mixed into one stream, system.wav. After the
call, sherpa-onnx tells their voices apart (speaker diarization: pyannote's
segmentation model finds the turns, a speaker-embedding model groups them by
voice), and each of their lines is relabelled Speaker 1, Speaker 2… by which
voice was speaking when it was said. One voice stays "Them".

The names come later, from the user: renaming "Speaker 1" to "Anna" rewrites the
meeting's notes in place and is remembered for its transcript, so the model
never has to run again for it.
"""
import logging
import re
import tarfile
from array import array
from collections import Counter
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


def relabel(lines: list, folder: Path, status=lambda text: None) -> list:
    """Live's last step: their lines by voice. On any failure the lines stay as
    they were — a call with one "Them" is still worth its notes."""
    if not any(line.who == "them" for line in lines):
        return lines
    try:
        ensure_models(status)
        status("telling the speakers apart")
        return assign(lines, diarize(Path(folder) / "system.wav"))
    except Exception as e:
        log.error("Could not tell the speakers apart in %s: %s", Path(folder).name, e)
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
