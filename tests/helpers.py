"""Shared test inputs and client helpers; no collected tests or production writes."""
import pytest

from worldmind.domain import canonical
from worldmind.engine import Command
from worldmind.planner import Budget, decide
from worldmind.providers import PROFILES, Settings


def settings(name):
    p = PROFILES[name]
    return Settings(name, "test-model", p.endpoint or
                    ("http://localhost:8001/v1/chat/completions" if p.local else
                     "https://resource.openai.azure.com/openai/deployments/test/chat/completions?api-version=test"),
                    "fixture-secret", timeout=0.2)


def proposal_for(e):
    actions = decide(e.context(), [], "Full", Budget(3000)).plan
    return canonical({"actions": [a.model_dump() for a in actions]})


def response_for(protocol, text, usage=True):
    if protocol == "chat":
        data = {"choices": [{"finish_reason": "stop", "message": {"content": text}}]}
        if usage:
            data["usage"] = {"prompt_tokens": 20, "completion_tokens": 10,
                             "prompt_tokens_details": {"cached_tokens": 3}, "prompt_cache_hit_tokens": 3}
    elif protocol == "responses":
        data = {"status": "completed", "output": [{"type": "message",
                "content": [{"type": "output_text", "text": text}]}]}
        if usage:
            data["usage"] = {"input_tokens": 20, "output_tokens": 10}
    elif protocol == "messages":
        data = {"stop_reason": "end_turn", "content": [{"type": "text", "text": text}]}
        if usage:
            data["usage"] = {"input_tokens": 20, "output_tokens": 10, "cache_read_input_tokens": 3}
    elif protocol == "gemini":
        data = {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": text}]}}]}
        if usage:
            data["usageMetadata"] = {"promptTokenCount": 20, "candidatesTokenCount": 10,
                                     "thoughtsTokenCount": 7, "totalTokenCount": 37}
    else:
        data = {"done": True, "done_reason": "stop", "message": {"content": text}}
        if usage:
            data.update(prompt_eval_count=20, eval_count=10)
    return data


def new(client):
    response = client.post("/api/sessions", json={})
    assert response.status_code == 201
    data = response.json()
    return f'/api/sessions/{data["id"]}', {"x-session-token": data["token"]}, data


def mutate(client, path, headers, command, revision=0, command_id="a"):
    return client.post(path + "/commands", headers=headers, json={
        "command_id": command_id, "expected_revision": revision, "command": command})


def finish(engine):
    """Bound completion helpers so a runtime regression cannot hang the suite."""
    # One call may mark the tick limit, then one iteration observes that terminal state.
    for _ in range(engine.config.max_ticks + 2):
        if engine.status not in ("ready", "running"):
            return engine
        engine.advance(Command(kind="step"))
    pytest.fail("unbounded runtime")
