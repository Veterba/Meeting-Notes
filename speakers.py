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
from dataclasses import replace
from pathlib import Path

import paths
from transcriber import WavTail, download

log = logging.getLogger("pipeline")

RELEASES = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
SEGMENTATION_URL = (f"{RELEASES}/speaker-segmentation-models/"
                    "sherpa-onnx-pyannote-segmentation-3-0.tar.bz2")
EMBEDDING_NAME = "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx"
EMBEDDING_URL = f"{RELEASES}/speaker-recongition-models/{EMBEDDING_NAME}"   # sic, their tag
# How alike two voices must be to count as one person. Chosen on test voices
# from the middle of the range where the result did not change (0.3–0.5).
THRESHOLD = 0.5
NAMES = "_speakers"             # in a meeting's notes: {"Speaker 1": "Anna", ...}


def models_dir() -> Path:
    return paths.DATA / "models" / "speakers"


def segmentation_model() -> Path:
    return models_dir() / "sherpa-onnx-pyannote-segmentation-3-0" / "model.int8.onnx"


def embedding_model() -> Path:
    return models_dir() / EMBEDDING_NAME


def ensure_models(status=lambda text: None):
    """Download the two small models (about 30 MB) the first time."""
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
    """Their lines relabelled by the voice that overlaps each the most.

    Voices are numbered by when they first speak, so Speaker 1 opens the call.
    A line no voice overlaps keeps the speaker before it. Mine are untouched.
    """
    def voice(line):
        best, most = None, 0.0
        for who, start, end in turns:
            overlap = min(end, line.end) - max(start, line.start)
            if overlap > most:
                best, most = who, overlap
        return best

    voices = [voice(line) if line.who == "them" else None for line in lines]
    heard = [v for v in voices if v is not None]
    if len(set(heard)) < 2:
        return lines                    # one voice on their side: "Them" says it
    number, last, out = {}, None, []
    for line, v in zip(lines, voices):
        if line.who != "them":
            out.append(line)
            continue
        v = last if v is None else v
        last = v
        if v is None:
            out.append(line)
            continue
        number.setdefault(v, len(number) + 1)
        out.append(replace(line, who=f"Speaker {number[v]}"))
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
