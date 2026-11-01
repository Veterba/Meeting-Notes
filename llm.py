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
def url() -> str:
    return paths.setting("OLLAMA_URL").rstrip("/")


def timeout() -> int:
    return int(paths.number("REQUEST_TIMEOUT"))


# The threshold is not the size of the model file but weights plus the KV cache
# for NUM_CTX, and all of it has to fit in memory at once: 14b at 32k context is
# ~12 GB resident, which on a 16 GB machine swaps (measured: a three-turn file
# took three minutes instead of fifteen seconds). Check with `ollama ps` —
# anything other than "100% GPU" in the PROCESSOR column means offload to CPU.
MODEL_BIG = "qwen3:30b-a3b"      # 24 GB and up
MODEL_MID = "qwen3:14b"          # 20 GB and up
MODEL_SMALL = "qwen3.5:9b"       # below 20 GB
ACTION_ITEM = {
    "type": "object",
    "properties": {
        "owner": {"type": "string"},
        "task": {"type": "string"},
        "deadline": {"type": ["string", "null"]},
    },
    "required": ["owner", "task", "deadline"],
}

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "date": {"type": "string"},
        "participants": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
        "decisions": {"type": "array", "items": {"type": "string"}},
        "action_items": {"type": "array", "items": ACTION_ITEM},
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
        "action_items": {"type": "array", "items": ACTION_ITEM},
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
    if ram >= paths.number("RAM_FOR_BIG"):
        return MODEL_BIG
    if ram >= paths.number("RAM_FOR_MID"):
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


def has_model(name: str, installed: list = None) -> bool:
    """Is the model downloaded? Ollama lists a bare name with ":latest" added."""
    models = installed_models() if installed is None else installed
    return name in models or f"{name}:latest" in models


def model_choices(installed: list) -> list:
    """What the menu offers: the current model, the three sizes, then the rest."""
    choices = []
    for name in [default_model(), MODEL_SMALL, MODEL_MID, MODEL_BIG] + installed:
        if name and name not in choices:
            choices.append(name)
    return choices


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


def with_schema(prompt: str, schema: dict) -> str:
    """The prompt, for a model that cannot be held to a schema: told it instead."""
    return (prompt + "\n\nAnswer with ONLY one JSON object, no prose and no code fence, "
            "matching this JSON schema:\n" + json.dumps(schema, ensure_ascii=False))


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
        "options": {"num_ctx": int(paths.number("NUM_CTX")),
                    "temperature": paths.number("TEMPERATURE")},
    }
    endpoint, limit = f"{url()}/api/generate", timeout()
    last_err = None
    for attempt in (1, 2):
        r = requests.post(endpoint, json=payload, timeout=limit)
        if r.status_code == 400 and "think" in r.text.lower():
            payload.pop("think", None)   # model has no thinking mode
            r = requests.post(endpoint, json=payload, timeout=limit)
        if r.status_code == 501 and "structured output" in r.text.lower():
            # Some model and Ollama builds (qwen3.5 on Ollama 0.40) cannot be
            # constrained by a schema at all. The schema goes into the prompt
            # instead, and _extract_json reads what comes back.
            payload.pop("format", None)
            prompt = with_schema(prompt, schema)
            payload["prompt"] = prompt + "\n/no_think"
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
    max_tokens = int(max_tokens if max_tokens is not None else paths.number("CHUNK_TOKENS"))
    overlap = int(overlap if overlap is not None else paths.number("OVERLAP_TURNS"))
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
    from parser import UNKNOWN, speakers, to_text

    model = model or default_model()
    full = to_text(turns)
    named = [s for s in speakers(turns) if s != UNKNOWN]
    who = (", ".join(named) if meta.get("named", True) else
           "not named in this transcript. An owner is only a name spoken in the "
           "text; when nobody is named for a task, leave the owner empty.")
    meta_line = (f"File: {meta.get('source', '')}\n"
                 f"File date: {meta.get('date', '')}\n"
                 f"Speakers: {who}")

    def report(stage, i=0, n=0):
        if progress_cb:
            progress_cb(stage, i, n)

    if estimate_tokens(full) <= paths.number("MAP_THRESHOLD_TOKENS"):
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
        result["participants"] = named
    if not result.get("date"):
        result["date"] = meta.get("date", "")
    if not result.get("title"):
        result["title"] = meta.get("source", "Meeting")
    result["_meta"] = {"model": model, "source": meta.get("source", ""),
                       "turns": len(turns), "tokens_est": estimate_tokens(full)}
    return result
