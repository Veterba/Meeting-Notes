"""The one call to Ollama, with its fallbacks. requests is replaced; no model runs."""
import json
from types import SimpleNamespace

import llm

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
