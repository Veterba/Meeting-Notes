"""The one call to Ollama, with its fallbacks. requests is replaced; no model runs."""
import json
from types import SimpleNamespace

import llm
from helpers import wait_until

SCHEMA = {"type": "object", "properties": {"title": {"type": "string"}}}


def answers(*replies):
    """A requests.post that answers in turn, and keeps what it was sent."""
    sent = []

    def post(url, json=None, timeout=None):
        sent.append(dict(json))
        code, body = replies[len(sent) - 1]
        return SimpleNamespace(status_code=code, text=body,
                               json=lambda: {"response": body},
                               raise_for_status=lambda: None)
    return post, sent


def test_a_model_without_structured_output_is_told_the_schema_instead(monkeypatch):
    post, sent = answers((501, '{"error":"structured output is unavailable"}'),
                         (200, 'Sure! {"title": "Sprint review"}'))
    monkeypatch.setattr(llm.requests, "post", post)

    assert llm.generate_json("Summarise.", SCHEMA, "qwen3.5:9b") == {"title": "Sprint review"}

    assert "format" in sent[0] and "format" not in sent[1]
    assert json.dumps(SCHEMA) in sent[1]["prompt"]


def test_a_model_with_structured_output_gets_the_schema_as_the_format(monkeypatch):
    post, sent = answers((200, '{"title": "x"}'))
    monkeypatch.setattr(llm.requests, "post", post)

    llm.generate_json("Summarise.", SCHEMA, "qwen3:14b")

    assert sent[0]["format"] == SCHEMA and len(sent) == 1


# ---------------------------------------------------------------- Ollama on demand

def test_the_model_is_found_on_disk_without_ollama_running(tmp_path, monkeypatch):
    monkeypatch.setenv("OLLAMA_MODELS", str(tmp_path))
    manifest = tmp_path / "manifests/registry.ollama.ai/library/qwen3.5/9b"
    assert not llm.model_on_disk("qwen3.5:9b")
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}")
    assert llm.model_on_disk("qwen3.5:9b")
    assert not llm.model_on_disk("qwen3.5:27b")


def test_an_ollama_the_app_started_is_stopped_again(tmp_path, monkeypatch):
    import subprocess
    import paths
    monkeypatch.setattr(paths, "DATA", tmp_path)
    fake = tmp_path / "ollama"
    fake.write_text("#!/bin/sh\necho serving\nexec sleep 60\n")
    fake.chmod(0o755)
    monkeypatch.setattr(llm, "ollama_binary", lambda: str(fake))
    started = []
    real_popen = subprocess.Popen
    monkeypatch.setattr(llm.subprocess, "Popen",
                        lambda *a, **kw: started.append(a[0]) or real_popen(*a, **kw))
    monkeypatch.setattr(llm, "ollama_up", lambda: bool(started))     # up once started
    posted = []
    monkeypatch.setattr(llm.requests, "post", lambda *a, **kw: posted.append(kw["json"]))

    own = llm.Ollama()
    assert own.ensure(timeout=5)
    assert started == [[str(fake), "serve"]]
    proc = own.proc
    # What it says goes to a log, so a failed start can be read about later.
    assert wait_until(lambda: "serving" in (tmp_path / "logs" / "ollama.log").read_text())
    own.release()

    assert proc.poll() is not None, "the app's own Ollama is still running"
    assert posted == []                 # stopping it unloads everything anyway


def test_someone_elses_ollama_keeps_running_and_only_the_model_goes(monkeypatch):
    monkeypatch.setattr(llm, "ollama_up", lambda: True)
    posted = []
    monkeypatch.setattr(llm.requests, "post", lambda *a, **kw: posted.append(kw["json"]))

    other = llm.Ollama()
    assert other.ensure() and other.proc is None
    other.release()

    assert posted == [{"model": llm.MODEL, "keep_alive": 0}]


def test_without_ollama_installed_nothing_starts(monkeypatch):
    monkeypatch.setattr(llm, "ollama_up", lambda: False)
    monkeypatch.setattr(llm, "ollama_binary", lambda: "")
    assert not llm.Ollama().ensure()
