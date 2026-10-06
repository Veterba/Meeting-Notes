"""Local Ollama: model choice, map-reduce, schema-constrained JSON."""
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

import requests

import paths

# "Ollama is not running" as opposed to "Ollama refused": a job that hits this is
# held and retried rather than marked failed.
UNREACHABLE = requests.exceptions.ConnectionError

# Settings are read when they are used, not at import: the model or the Ollama
# address can change in the menu while the app is running.
DEFAULTS = {"OLLAMA_URL": "http://localhost:11434", "NUM_CTX": 32768,
            "TEMPERATURE": 0.2, "CHUNK_TOKENS": 5000, "MAP_THRESHOLD_TOKENS": 6000,
            "OVERLAP_TURNS": 2, "REQUEST_TIMEOUT": 1800,
            "RAM_FOR_BIG": 24, "RAM_FOR_MID": 20}


def url() -> str:
    return paths.setting("OLLAMA_URL", DEFAULTS["OLLAMA_URL"]).rstrip("/")


def num(key: str) -> float:
    return paths.setting_float(key, DEFAULTS[key])


def timeout() -> int:
    return int(num("REQUEST_TIMEOUT"))


# The threshold is not the size of the model file but weights plus the KV cache
# for NUM_CTX, and all of it has to fit in memory at once: 14b at 32k context is
# ~12 GB resident, which on a 16 GB machine swaps (measured: a three-turn file
# took three minutes instead of fifteen seconds). Check with `ollama ps` —
# anything other than "100% GPU" in the PROCESSOR column means offload to CPU.
MODEL_BIG = "qwen3:30b-a3b"      # 24 GB and up
MODEL_MID = "qwen3:14b"          # 20 GB and up
MODEL_SMALL = "qwen3.5:9b"       # below 20 GB
SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "date": {"type": "string"},
        "participants": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
        "decisions": {"type": "array", "items": {"type": "string"}},
        "action_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "task": {"type": "string"},
                    "deadline": {"type": ["string", "null"]},
                },
                "required": ["owner", "task", "deadline"],
            },
        },
        "assumptions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "statement": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                    "evidence_quote": {"type": "string"},
                },
                "required": ["statement", "confidence", "evidence_quote"],
            },
        },
        "open_questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "title", "date", "participants", "summary", "decisions",
        "action_items", "assumptions", "open_questions",
    ],
}

MAP_SCHEMA = {
    "type": "object",
    "properties": {
        "facts": {"type": "array", "items": {"type": "string"}},
        "decisions": {"type": "array", "items": {"type": "string"}},
        "action_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "task": {"type": "string"},
                    "deadline": {"type": ["string", "null"]},
                },
                "required": ["owner", "task", "deadline"],
            },
        },
        "quotes": {"type": "array", "items": {"type": "string"}},
        "open_questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["facts", "decisions", "action_items", "quotes", "open_questions"],
}


# ---------------------------------------------------------------- environment

def ram_gb() -> float:
    try:
        out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True)
        return int(out.stdout.strip()) / (1024 ** 3)
    except Exception:
        return 0.0


def default_model() -> str:
    chosen = paths.setting("OLLAMA_MODEL").strip()
    if chosen:
        return chosen
    ram = ram_gb()
    if ram >= num("RAM_FOR_BIG"):
        return MODEL_BIG
    if ram >= num("RAM_FOR_MID"):
        return MODEL_MID
    return MODEL_SMALL


def ollama_up() -> bool:
    try:
        return requests.get(f"{url()}/api/tags", timeout=3).ok
    except Exception:
        return False


def ollama_binary() -> str:
    """Find the ollama executable.

    A bundled .app inherits a bare PATH from launchd, so `which` alone is not
    enough — the usual Homebrew locations are checked explicitly.
    """
    for candidate in ("/opt/homebrew/bin/ollama", "/usr/local/bin/ollama",
                      shutil.which("ollama")):
        if candidate and Path(candidate).exists():
            return candidate
    return ""


def start_ollama(timeout: float = 30) -> bool:
    """Bring Ollama up if it is not already, so the user never needs a terminal."""
    if ollama_up():
        return True
    exe = ollama_binary()
    if not exe:
        return False
    subprocess.Popen([exe, "serve"], stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if ollama_up():
            return True
        time.sleep(0.5)
    return False


def installed_models() -> list:
    try:
        r = requests.get(f"{url()}/api/tags", timeout=5)
        return [m["name"] for m in r.json().get("models", [])]
    except Exception:
        return []


def has_model(name: str) -> bool:
    models = installed_models()
    return name in models or f"{name}:latest" in models


def pull_model(name: str, progress_cb=None):
    """Download a model, reporting progress_cb(status, done_bytes, total_bytes)."""
    r = requests.post(f"{url()}/api/pull", json={"model": name, "stream": True},
                      stream=True, timeout=timeout())
    r.raise_for_status()
    for line in r.iter_lines():
        if not line:
            continue
        ev = json.loads(line)
        if ev.get("error"):
            raise RuntimeError(ev["error"])
        if progress_cb:
            progress_cb(ev.get("status", ""), ev.get("completed", 0), ev.get("total", 0))
    return True


# ---------------------------------------------------------------- generation

def _strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


def _extract_json(text: str):
    text = _strip_think(text)
    try:
        return json.loads(text)
    except Exception:
        pass
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON in the model response")
    depth, in_str, esc = 0, False, False
    for i, ch in enumerate(text[start:], start):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("unterminated JSON in the model response")


def generate_json(prompt: str, schema: dict, model: str) -> dict:
    """One /api/generate call constrained by a JSON schema, with a single retry.

    The schema goes in `format`, so Ollama restricts generation by grammar rather
    than asking the model nicely. Qwen3's thinking mode is switched off twice over:
    `think: false` and a /no_think suffix, because either one alone fails on some
    Ollama or model builds. A model without thinking support answers 400, which is
    handled by dropping the parameter and retrying.
    """
    payload = {
        "model": model,
        "prompt": prompt + "\n/no_think",
        "stream": False,
        "format": schema,
        "think": False,
        "options": {"num_ctx": int(num("NUM_CTX")), "temperature": num("TEMPERATURE")},
    }
    endpoint, limit = f"{url()}/api/generate", timeout()
    last_err = None
    for attempt in (1, 2):
        r = requests.post(endpoint, json=payload, timeout=limit)
        if r.status_code == 400 and "think" in r.text.lower():
            payload.pop("think", None)   # model has no thinking mode
            r = requests.post(endpoint, json=payload, timeout=limit)
        r.raise_for_status()
        raw = r.json().get("response", "")
        try:
            return _extract_json(raw)
        except Exception as e:
            last_err = e
            payload["options"]["temperature"] = 0.0
            payload["prompt"] = (
                prompt + "\n\nReturn ONLY valid JSON matching the schema, "
                "no prose.\n/no_think")
    raise ValueError(f"model returned no valid JSON after 2 attempts: {last_err}")


# ---------------------------------------------------------------- map-reduce

def estimate_tokens(text: str) -> int:
    """Rough but good enough to compare against a threshold: ~3 chars per token."""
    return max(1, len(text) // 3)


def chunk_turns(turns, max_tokens=None, overlap=None):
    """Split on turn boundaries, never mid-sentence, with a small overlap.

    Cutting inside a sentence makes nonsense of both halves, and the model then
    invents meaning to fill the gap. The overlap keeps a decision voiced across
    a chunk boundary from falling between two chunks.
    """
    max_tokens = int(max_tokens if max_tokens is not None else num("CHUNK_TOKENS"))
    overlap = int(overlap if overlap is not None else num("OVERLAP_TURNS"))
    chunks, cur, cur_tokens = [], [], 0
    for turn in turns:
        t = estimate_tokens(f"{turn[0]}: {turn[1]}")
        if cur and cur_tokens + t > max_tokens:
            chunks.append(cur)
            cur = cur[-overlap:] if overlap else []
            cur_tokens = sum(estimate_tokens(f"{s}: {x}") for s, x in cur)
        cur.append(turn)
        cur_tokens += t
    if cur:
        chunks.append(cur)
    return chunks


def _prompt(name: str, **vars_) -> str:
    """Load a prompt and substitute {{PLACEHOLDER}}s.

    Plain str.replace rather than Jinja2: the prompts are full of curly braces
    from the JSON examples, which a template engine would trip over.
    """
    text = (paths.CODE / "prompts" / f"{name}.md").read_text(encoding="utf-8")
    for k, v in vars_.items():
        text = text.replace("{{" + k + "}}", str(v))
    return text


def summarize(turns, meta: dict, model: str = None, progress_cb=None) -> dict:
    """Turns -> the final JSON described by SUMMARY_SCHEMA."""
    from parser import to_text, speakers

    model = model or default_model()
    full = to_text(turns)
    meta_line = (f"File: {meta.get('source', '')}\n"
                 f"File date: {meta.get('date', '')}\n"
                 f"Speakers: {', '.join(speakers(turns))}")

    def report(stage, i=0, n=0):
        if progress_cb:
            progress_cb(stage, i, n)

    if estimate_tokens(full) <= num("MAP_THRESHOLD_TOKENS"):
        report("reduce", 1, 1)
        result = generate_json(
            _prompt("reduce", META=meta_line, NOTES="", TRANSCRIPT=full), SUMMARY_SCHEMA, model)
    else:
        chunks = chunk_turns(turns)
        notes = []
        for i, chunk in enumerate(chunks, 1):
            report("map", i, len(chunks))
            part = generate_json(
                _prompt("map", META=meta_line, PART_INDEX=i, TOTAL_PARTS=len(chunks),
                        CHUNK=to_text(chunk)), MAP_SCHEMA, model)
            notes.append({"part": i, **part})
        report("reduce", len(chunks), len(chunks))
        result = generate_json(
            _prompt("reduce", META=meta_line,
                    NOTES=json.dumps(notes, ensure_ascii=False, indent=2),
                    TRANSCRIPT=""), SUMMARY_SCHEMA, model)

    if not result.get("participants"):
        result["participants"] = speakers(turns)
    if not result.get("date"):
        result["date"] = meta.get("date", "")
    if not result.get("title"):
        result["title"] = meta.get("source", "Meeting")
    result["_meta"] = {"model": model, "source": meta.get("source", ""),
                       "turns": len(turns), "tokens_est": estimate_tokens(full)}
    return result
