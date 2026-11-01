"""Turning a recording into text while the call is still going.

The Swift helper writes system.wav (them) and mic.wav (me) as 16 kHz mono
16-bit and keeps appending to both. `Live` reads them as they grow, cuts each
stream into utterances at its pauses, and has a local whisper.cpp server
transcribe them one at a time. The lines show in the window during the call;
when it ends they are written out as a .vtt, which the pipeline summarises like
any downloaded transcript.

Echo: without headphones the others' voices come back in through the
microphone. The helper's voice processing removes most of it; what is left is
caught here — a line of mine whose words are mostly a line of theirs from the
same moment is their line heard twice, and is dropped.
"""
import io
import logging
import math
import queue
import re
import shutil
import socket
import subprocess
import threading
import time
import wave
from array import array
from dataclasses import asdict, dataclass
from pathlib import Path

import requests

import paths

log = logging.getLogger("pipeline")

RATE = 16000
FRAME = 480                     # 30 ms
MODEL_NAME = "ggml-large-v3-turbo-q5_0.bin"
MODEL_URL = f"https://huggingface.co/ggerganov/whisper.cpp/resolve/main/{MODEL_NAME}"
VAD_NAME = "ggml-silero-v5.1.2.bin"
VAD_URL = f"https://huggingface.co/ggml-org/whisper-vad/resolve/main/{VAD_NAME}"
WHO = {"me": "Me", "them": "Them"}


# ---------------------------------------------------------------- reading

class WavTail:
    """A 16-bit mono WAV that is still being written, read from where it left off."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.offset = None              # where the next unread byte is
        self.carry = b""                # half a sample, kept for the next read

    @staticmethod
    def _data_offset(head: bytes):
        if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            return None
        pos = 12
        while pos + 8 <= len(head):
            chunk, size = head[pos:pos + 4], int.from_bytes(head[pos + 4:pos + 8], "little")
            if chunk == b"data":
                return pos + 8
            pos += 8 + size + (size & 1)
        return None

    def read(self) -> array:
        samples = array("h")
        if not self.path.exists():
            return samples
        with open(self.path, "rb") as f:
            if self.offset is None:
                self.offset = self._data_offset(f.read(65536))
                if self.offset is None:
                    return samples          # the header is not written yet
            f.seek(self.offset)
            data = f.read()
        self.offset += len(data)
        data, self.carry = self.carry + data, b""
        if len(data) % 2:
            data, self.carry = data[:-1], data[-1:]
        samples.frombytes(data)
        return samples


class Segmenter:
    """Cuts one stream into utterances at its pauses.

    Plain energy against a running noise floor: good enough to find the gaps
    between sentences. Whether a piece holds speech at all is decided later by
    whisper.cpp's own voice activity model.
    """
    PAUSE = 24                  # frames of quiet that end an utterance: 0.72 s
    LONGEST = 15 * RATE         # a monologue is cut every 15 s all the same
    SHORTEST = 10               # frames of sound for an utterance to count: 0.3 s
    PREROLL = 8                 # frames kept from before the sound started
    MIN_LEVEL = 300

    def __init__(self):
        self.seen = 0               # samples fed so far
        self.pending = array("h")   # less than a frame, waiting for the rest
        self.current = None         # the utterance being collected
        self.start = 0
        self.quiet = 0
        self.voiced = 0
        self.before = []            # the last few frames, for the pre-roll
        self.floor = self.MIN_LEVEL / 3      # rises to the room's noise within seconds

    def feed(self, samples: array) -> list:
        """Returns the utterances that ended in these samples: (start s, end s, samples)."""
        self.pending.extend(samples)
        done = []
        while len(self.pending) >= FRAME:
            frame, self.pending = self.pending[:FRAME], self.pending[FRAME:]
            at = self.seen
            self.seen += FRAME
            loud = self._loud(frame)
            if self.current is None:
                if loud:
                    self.current = array("h")
                    for old in self.before:
                        self.current.extend(old)
                    self.start = at - FRAME * len(self.before)
                    self.current.extend(frame)
                    self.quiet, self.voiced = 0, 1
                else:
                    self.before = (self.before + [frame])[-self.PREROLL:]
                continue
            self.current.extend(frame)
            self.quiet = 0 if loud else self.quiet + 1
            self.voiced += loud
            if self.quiet >= self.PAUSE or len(self.current) >= self.LONGEST:
                done += self._close()
        return done

    def open_piece(self):
        """The utterance still being spoken, so far: (start s, end s, samples), or None."""
        if self.current is None or self.voiced < self.SHORTEST:
            return None
        end = self.start + len(self.current)
        return (self.start / RATE, end / RATE, array("h", self.current))

    def flush(self) -> list:
        """The utterance still open when the recording stops."""
        return self._close() if self.current is not None else []

    def _close(self) -> list:
        piece, voiced, start = self.current, self.voiced, self.start
        self.current, self.before = None, []
        if voiced < self.SHORTEST:
            return []
        return [(start / RATE, (start + len(piece)) / RATE, piece)]

    def _loud(self, frame) -> bool:
        level = math.sqrt(sum(s * s for s in frame) / len(frame))
        # The floor follows quiet at once and noise slowly, so a long stretch
        # of speech never becomes the new floor.
        if level < self.floor:
            self.floor = level
        else:
            self.floor += (level - self.floor) * 0.0002   # ~minutes to follow a noisier room
        return level > max(self.MIN_LEVEL, self.floor * 3)


# ---------------------------------------------------------------- what came out

@dataclass
class Line:
    who: str            # "me" or "them"
    start: float        # seconds from the start of the recording
    end: float
    text: str


def words(text: str) -> list:
    return re.findall(r"\w+", text.lower())


def echoes(mine: Line, theirs: Line) -> bool:
    """Is my line just their line, heard again through the microphone?"""
    if mine.end < theirs.start - 2 or mine.start > theirs.end + 2:
        return False
    a, b = words(mine.text), set(words(theirs.text))
    return bool(a) and sum(w in b for w in a) / len(a) >= 0.6


# What Whisper says over silence and music, in the two languages the calls are in.
NOT_SPEECH = re.compile(
    r"^\W*(thank you\.?|thanks for watching!?|you|"
    r"продолжение следует\.*|субтитры .*|редактор субтитров.*|спасибо за просмотр!?)\W*$"
    r"|^\s*[\[(].*[\])]\s*$", re.I)


def is_speech(text: str) -> bool:
    return bool(words(text)) and not NOT_SPEECH.match(text)


def stamp(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d}.{ms % 1000:03d}"


def to_vtt(lines: list) -> str:
    def esc(s):
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    cues = [f"{stamp(line.start)} --> {stamp(line.end)}\n<v {WHO[line.who]}>{esc(line.text)}</v>"
            for line in lines]
    return "WEBVTT\n\n" + "\n\n".join(cues) + "\n"


# ---------------------------------------------------------------- whisper.cpp

def model_path() -> Path:
    raw = paths.setting("WHISPER_MODEL")
    return Path(raw).expanduser() if raw else paths.DATA / "models" / MODEL_NAME


def vad_path() -> Path:
    return model_path().parent / VAD_NAME


def server_binary() -> str:
    """whisper-server from Homebrew. An app opened from Finder has no Homebrew
    in its PATH, so the usual places are tried by name too."""
    for candidate in (shutil.which("whisper-server"), "/opt/homebrew/bin/whisper-server",
                      "/usr/local/bin/whisper-server"):
        if candidate and Path(candidate).exists():
            return candidate
    return ""


def download(url: str, dest: Path, progress=None):
    """Fetch a model file, through a .part file so a broken download never
    looks like a model."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        total, done = int(r.headers.get("Content-Length") or 0), 0
        with open(part, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    part.rename(dest)


def wav_bytes(samples: array) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(samples.tobytes())
    return buf.getvalue()


class WhisperServer:
    """A whisper.cpp server of our own, with the model loaded once for the call."""

    def __init__(self):
        self.proc = None
        self.url = ""

    def start(self, status=lambda text: None):
        binary = server_binary()
        if not binary:
            raise RuntimeError("whisper.cpp is not installed. Install it: brew install whisper-cpp")
        for url, dest in ((MODEL_URL, model_path()), (VAD_URL, vad_path())):
            if not dest.exists():
                log.info("Downloading %s", dest.name)
                download(url, dest, lambda done, total, n=dest.name: status(
                    f"downloading the speech model {done >> 20}/{total >> 20} MB"))
        status("starting the speech model")
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.proc = subprocess.Popen(
            [binary, "-m", str(model_path()), "--host", "127.0.0.1", "--port", str(port),
             "-l", "auto", "--vad", "-vm", str(vad_path())],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.url = f"http://127.0.0.1:{port}"
        for _ in range(120):
            if self.proc.poll() is not None:
                raise RuntimeError("the speech model would not start")
            try:
                requests.get(self.url, timeout=1)
                return
            except requests.RequestException:
                time.sleep(0.5)
        raise RuntimeError("the speech model did not answer")

    def transcribe(self, samples: array) -> str:
        language = paths.setting("WHISPER_LANGUAGE") or "auto"
        r = requests.post(f"{self.url}/inference",
                          files={"file": ("piece.wav", wav_bytes(samples), "audio/wav")},
                          data={"response_format": "json", "language": language,
                                "temperature": "0"},
                          timeout=300)
        r.raise_for_status()
        return (r.json().get("text") or "").strip()

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None


# ---------------------------------------------------------------- one recording

class Live:
    """One recording's transcription, from its first second to the .vtt.

    Two threads: a reader that turns the growing files into utterances every
    half second, and a transcriber that sends them to whisper one at a time.
    `transcribe` is replaceable, for the tests.

    A finished utterance only exists once its speaker pauses. So that words show
    while they are still being said, the transcriber, whenever it has no finished
    utterance waiting, transcribes the one still being spoken as it is so far: a
    partial line, replaced by the real one at the pause.
    """
    POLL = 0.5
    PARTIAL_EVERY = 0.8             # seconds of new speech before a partial is redone
    PARTIAL_SHORTEST = 1.0          # less than this is too little for Whisper to guess

    def __init__(self, folder: Path, transcribe=None):
        self.folder = Path(folder)
        self.streams = {"me": (WavTail(self.folder / "mic.wav"), Segmenter()),
                        "them": (WavTail(self.folder / "system.wav"), Segmenter())}
        self.server = None if transcribe else WhisperServer()
        self._transcribe = transcribe or self.server.transcribe
        self.lines = []
        self.partial = {}               # who -> Line, the sentence still being said
        self._open = {}                 # who -> the reader's latest open piece
        # Changes whenever the lines do. Starts from the clock, so a new call
        # never repeats a number the page still holds from the last one.
        self.version = time.monotonic_ns()
        self.status = "starting"
        self.error = ""
        self._lock = threading.Lock()
        self._pieces = queue.Queue()
        self._stopping = threading.Event()
        self._threads = []

    def start(self):
        for target, name in ((self._read_loop, "live-read"), (self._transcribe_loop, "live-text")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def snapshot(self, known: int = -1) -> dict:
        """The status, and the lines unless the caller already has this version.
        Lines can be dropped (an echo) or land before others (the two streams
        are transcribed in turn), so it is always the whole list or nothing."""
        with self._lock:
            same = known == self.version
            return {"status": self.status, "error": self.error, "count": len(self.lines),
                    "version": self.version,
                    "lines": None if same else [asdict(line) for line in self.lines],
                    "partial": None if same else [
                        asdict(p) for p in sorted(self.partial.values(), key=lambda p: p.start)]}

    def finish_all(self) -> Path:
        """Transcribe a finished recording from start to end, in one go."""
        self._stopping.set()
        self.start()
        return self.finish()

    def finish(self, timeout: float = None) -> Path:
        """Transcribe what is left, write the .vtt and return it — or None when
        nothing was said. Blocks until the last piece is done."""
        self._stopping.set()
        for t in self._threads:
            t.join(timeout)
        if self.server:
            self.server.stop()
        with self._lock:
            lines = list(self.lines)
            self.partial = {}
            self.status = "done"
        if not lines:
            return None
        out = self.folder / f"Call {self.folder.name}.vtt"
        out.write_text(to_vtt(lines), encoding="utf-8")
        return out

    # -------------------------------------------------- internals

    def _set(self, **values):
        with self._lock:
            for k, v in values.items():
                setattr(self, k, v)

    def _read_loop(self):
        while True:
            stopping = self._stopping.is_set()
            for who, (tail, segmenter) in self.streams.items():
                pieces = segmenter.feed(tail.read())
                if stopping:
                    pieces += segmenter.flush()
                for start, end, samples in pieces:
                    self._pieces.put((who, start, end, samples))
                with self._lock:
                    self._open[who] = None if stopping else segmenter.open_piece()
            if stopping:
                self._pieces.put(None)
                return
            self._stopping.wait(self.POLL)

    def _transcribe_loop(self):
        ready = True
        if self.server:
            try:
                self.server.start(status=lambda text: self._set(status=text))
            except Exception as e:
                # The audio is still being kept; only the text is lost.
                log.error("Live transcription unavailable: %s", e)
                self._set(status="no transcription", error=str(e))
                ready = False
        if ready:
            self._set(status="listening")
        while True:
            try:
                piece = self._pieces.get(timeout=self.POLL)
            except queue.Empty:
                if ready:
                    self._partials()
                continue
            if piece is None:
                return
            if not ready:
                continue
            who, start, end, samples = piece
            try:
                text = self._transcribe(samples)
            except Exception as e:
                log.error("Could not transcribe %.1f–%.1f s of %s: %s", start, end, who, e)
                continue
            if is_speech(text):
                self._add(Line(who, round(start, 2), round(end, 2), text))

    def _partials(self):
        """Transcribe what is being said right now, for each side that has grown
        enough since its last partial."""
        for who in ("them", "me"):
            with self._lock:
                piece = self._open.get(who)
                done = self.partial.get(who)
            if not piece:
                continue
            start, end, samples = piece
            if end - start < self.PARTIAL_SHORTEST or (done and done.start == round(start, 2)
                                                       and end - done.end < self.PARTIAL_EVERY):
                continue
            try:
                text = self._transcribe(samples)
            except Exception as e:
                log.error("Could not transcribe the open %s piece: %s", who, e)
                continue
            line = Line(who, round(start, 2), round(end, 2), text)
            with self._lock:
                # Only while that utterance is still the open one, and never an echo.
                still = self._open.get(who)
                echo = who == "me" and any(echoes(line, o) for o in
                                           [*self.lines, *self.partial.values()] if o.who == "them")
                if still and round(still[0], 2) == line.start and is_speech(text) and not echo:
                    self.partial[who] = line
                    self.version += 1

    def _add(self, line: Line):
        with self._lock:
            # The finished line takes over from its partial.
            if line.who in self.partial and self.partial[line.who].start <= line.start + 0.01:
                del self.partial[line.who]
                self.version += 1
            if line.who == "me" and any(echoes(line, other) for other in self.lines
                                        if other.who == "them"):
                return
            if line.who == "them":
                self.lines = [m for m in self.lines if m.who != "me" or not echoes(m, line)]
            self.lines.append(line)
            self.lines.sort(key=lambda x: x.start)
            self.version += 1
