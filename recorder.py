"""Recording a call. The audio is the Swift helper's job (recorder/main.swift);
this starts it, listens to what it says, stops it, and runs the live
transcription (`transcriber.Live`) alongside.

A recording is a folder in DATA/recordings named by when it started, holding
system.wav (everything the Mac played: the other people), mic.wav (me),
meta.json (formats and the start time) and, once it stops, the transcript as
"Call <folder>.vtt".
"""
import json
import logging
import subprocess
import threading
from datetime import datetime
from pathlib import Path

import paths
from transcriber import Live

log = logging.getLogger("pipeline")

STOP_SECONDS = 10
# A microphone always hears something; this many seconds of exact zero is a
# broken stream, not a quiet room.
DEAD_MIC_SECONDS = 5


class Recorder:
    make_live = Live                    # replaceable, for the tests

    def __init__(self):
        self.proc = None
        self.folder = None
        # Called on the reader thread as on_event(kind, folder, message), where
        # kind is started / error / transcribed (message: the .vtt, or "" when
        # nothing was said).
        self.on_event = None
        self.live = None                # the transcription of the current or last call
        self._lock = threading.Lock()
        self._state = self._idle()

    @staticmethod
    def _idle() -> dict:
        return {"recording": False, "transcribing": False, "started_at": 0.0, "seconds": 0.0,
                "system": 0.0, "mic": 0.0, "folder": "", "error": "", "warning": ""}

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._state)

    @property
    def recording(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> Path:
        """Start recording into a new folder. Raises if it is already running or
        the helper is missing."""
        if self.recording:
            raise RuntimeError("already recording")
        helper = paths.recorder_binary()
        if not helper.exists():
            raise RuntimeError(f"the recorder is not built ({helper})")
        folder = paths.recordings() / datetime.now().strftime("%Y-%m-%d %H-%M-%S")
        folder.mkdir(parents=True, exist_ok=True)
        self.folder = folder
        with self._lock:
            self._state = dict(self._idle(), recording=True, folder=str(folder))
        self.proc = subprocess.Popen([str(helper), str(folder)], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True, bufsize=1)
        threading.Thread(target=self._read, args=(self.proc, folder), name="recorder",
                         daemon=True).start()
        log.info("Recording into %s", folder)
        return folder

    def stop(self):
        """Ask the helper to finish its files, and wait for it."""
        proc = self.proc
        if not proc or proc.poll() is not None:
            return
        try:
            proc.stdin.close()              # the helper finishes on end of input
            proc.wait(STOP_SECONDS)
        except subprocess.TimeoutExpired:
            log.warning("The recorder did not stop by itself; terminating it")
            proc.terminate()
            proc.wait(STOP_SECONDS)

    # -------------------------------------------------- internals

    def _read(self, proc, folder: Path):
        error, meta, live, dead_mic = "", {}, None, 0
        for line in proc.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind = event.get("event")
            if kind == "started":
                meta = {k: event.get(k) for k in ("system", "mic", "at")}
                (folder / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
                self._update(started_at=event.get("at") or 0.0)
                live = self.live = self.make_live(folder)
                live.start()
                self._emit("started", folder, "")
            elif kind == "level":
                mic = event.get("mic", 0.0)
                self._update(system=event.get("system", 0.0), mic=mic,
                             seconds=self._elapsed(meta))
                dead_mic = dead_mic + 1 if mic == 0 and event.get("mic_frames") else 0
                if dead_mic == DEAD_MIC_SECONDS:
                    self._warn(folder, "the microphone is sending pure silence")
            elif kind == "warning":
                self._warn(folder, event.get("what", ""))
            elif kind == "error":
                error = event.get("what", "the recorder failed")
                log.error("Recorder: %s (status %s)", error, event.get("status"))
            elif kind == "stopped":
                self._update(seconds=event.get("seconds", 0.0))
        proc.wait()
        with self._lock:
            self._state.update(recording=False, transcribing=live is not None, error=error)
        log.info("Recording stopped: %s%s", folder.name, f" — {error}" if error else "")
        if error:
            self._emit("error", folder, error)
        if live:
            transcript = live.finish()
            self._update(transcribing=False)
            self._emit("transcribed", folder, str(transcript) if transcript else "")

    def _warn(self, folder: Path, what: str):
        log.warning("Recorder: %s", what)
        self._update(warning=what)
        self._emit("warning", folder, what)

    @staticmethod
    def _elapsed(meta: dict) -> float:
        at = meta.get("at")
        return datetime.now().timestamp() - at if at else 0.0

    def _update(self, **values):
        with self._lock:
            self._state.update(values)

    def _emit(self, kind, folder, message):
        if not self.on_event:
            return
        try:
            self.on_event(kind, folder, message)
        except Exception as e:      # a broken listener must not kill the reader
            log.error("Recorder listener failed on %s: %s", kind, e)
