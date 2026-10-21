import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from worldmind.domain import Config, canonical
from worldmind.engine import Engine
from worldmind.models import Ledger, ModelService
from worldmind.planner import Budget, decide
from worldmind.providers import (PROFILES, ProviderError, Settings, build_request,
                                 parse_proposal, parse_response, usage_of)


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


@pytest.mark.parametrize("name", list(PROFILES))
async def test_each_profile_request_auth_parse_audit_and_cache(name, tmp_path):
    e, s = Engine(), settings(name)
    seen = []
    async def handler(req):
        seen.append(req)
        assert req.method == "POST"
        body = json.loads(req.content)
        p = s.profile
        if p.auth == "bearer":
            assert req.headers["authorization"] == "Bearer fixture-secret"
        elif p.auth == "google":
            assert req.headers["x-goog-api-key"] == "fixture-secret"
        elif p.auth == "anthropic":
            assert req.headers["x-api-key"] == "fixture-secret"
            assert req.headers["anthropic-version"] == "2023-06-01"
        elif p.auth == "api-key":
            assert req.headers["api-key"] == "fixture-secret"
        else:
            assert "authorization" not in req.headers
        if p.protocol in ("chat", "messages", "responses"):
            assert body[p.token_field] == 1024
            assert body["stream"] is False
        elif p.protocol == "gemini":
            assert body["generationConfig"]["maxOutputTokens"] == 1024
            assert "model" not in body
        else:
            assert body["options"]["num_predict"] == 1024
        if name == "openrouter":
            assert body["provider"]["allow_fallbacks"] is False
        assert "fixture-secret" not in req.content.decode()
        assert "world_revision" not in req.content.decode()
        assert "merchant" not in req.content.decode()
        assert "temperature" not in body
        return httpx.Response(200, json=response_for(p.protocol, proposal_for(e)))
    ledger = Ledger(str(tmp_path / "audit.db"), limit=1)
    service = ModelService(ledger, enabled=True, transport=httpx.MockTransport(handler))
    result = await service.propose(s, e.request_context(), e.context())
    assert result["source"] == "provider_response"
    assert result["usage"]["input_tokens"] == 20
    assert result["usage"]["cost"] is None
    cached = await service.propose(s, e.request_context(), e.context())
    assert cached["source"] == "validated_cache" and len(seen) == 1
    summary = ledger.summary()
    assert summary["reserved_requests"] == 1 and len(summary["requests"]) == 2
    assert "fixture-secret" not in canonical(summary)
    assert len(ledger.summary()["requests"][0]["request_hash"]) == 64
    # New model/config cannot consume another account-wide allowance after restart.
    restarted = ModelService(Ledger(ledger.path, limit=1), enabled=True,
                             transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderError, match="quota"):
        await restarted.propose(replace(s, model="other-model"), e.request_context(), e.context())


@pytest.mark.parametrize("name", list(PROFILES))
@pytest.mark.parametrize("mode", ["http", "invalid", "missing_usage"])
async def test_each_profile_errors_and_unknown_usage(name, mode, tmp_path):
    e, s = Engine(), settings(name)
    calls = 0
    async def handler(req):
        nonlocal calls
        calls += 1
        if mode == "http":
            return httpx.Response(429, text="sensitive upstream fixture-secret")
        text = '{"actions":[{"kind":"move","target":"home"}]}' if mode == "invalid" else proposal_for(e)
        return httpx.Response(200, json=response_for(s.profile.protocol, text, usage=mode != "missing_usage"))
    ledger = Ledger(str(tmp_path / "ledger.db"))
    service = ModelService(ledger, enabled=True, transport=httpx.MockTransport(handler))
    if mode == "missing_usage":
        result = await service.propose(s, e.request_context(), e.context())
        assert result["usage"]["input_tokens"] is None and not result["usage"]["known"]
    else:
        with pytest.raises(ProviderError):
            await service.propose(s, e.request_context(), e.context())
        assert ledger.summary()["requests"][0]["status"] != "pending"
    assert calls == 1
    assert ledger.summary()["reserved_requests"] == 1
    assert "fixture-secret" not in canonical(ledger.summary())


@pytest.mark.parametrize("text", [
    '{"actions":[]}', '{"actions":[],"actions":[]}', '{"actions":[],"secret":"leak"}',
    '```json\n{"actions":[]}\n```', '{"actions":[{"kind":"move","target":"moon"}]}',
    '{"actions":NaN}', 'x' * 16385, '[[]]',
])
def test_strict_proposal(text):
    with pytest.raises(ProviderError):
        parse_proposal(text)


@pytest.mark.parametrize("protocol", ["chat", "responses", "messages", "gemini", "ollama"])
def test_truncation_refusal_and_tool_calls_not_accepted(protocol):
    data = response_for(protocol, proposal_for(Engine()))
    if protocol == "chat":
        data["choices"][0]["finish_reason"] = "length"
    elif protocol == "responses":
        data["status"] = "incomplete"
    elif protocol == "messages":
        data["stop_reason"] = "max_tokens"
    elif protocol == "gemini":
        data["candidates"][0]["finishReason"] = "SAFETY"
    else:
        data["done"] = False
    with pytest.raises(ProviderError):
        parse_response(protocol, data)


async def test_timeout_cancel_concurrency_and_no_refunds(tmp_path):
    e = Engine()
    started = asyncio.Event()
    async def handler(req):
        started.set()
        await asyncio.Event().wait()
    ledger = Ledger(str(tmp_path / "ledger.db"), limit=3)
    service = ModelService(ledger, enabled=True, transport=httpx.MockTransport(handler), concurrency=1)
    task = asyncio.create_task(service.propose(settings("deepseek"), e.request_context(), e.context()))
    await started.wait()
    with pytest.raises(ProviderError, match="concurrency"):
        await service.propose(settings("deepseek"), e.request_context(), e.context())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ledger.summary()["requests"][0]["status"] == "cancelled"
    with pytest.raises(ProviderError, match="timeout"):
        await service.propose(settings("deepseek"), e.request_context(), e.context())
    assert ledger.summary()["reserved_requests"] == 2
    assert ledger.summary()["requests"][0]["status"] == "timeout"


@pytest.mark.parametrize("mode", ["oversize", "redirect", "malformed", "network"])
async def test_transport_boundaries(mode, tmp_path):
    e = Engine()
    async def handler(req):
        if mode == "network":
            raise httpx.ConnectError("secret must never be reported")
        if mode == "redirect":
            return httpx.Response(302, headers={"location": "https://evil.invalid"})
        if mode == "oversize":
            return httpx.Response(200, content=b"x" * 150000)
        return httpx.Response(200, content=b"\xff")
    ledger = Ledger(str(tmp_path / "ledger.db"))
    service = ModelService(ledger, enabled=True, transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderError) as error:
        await service.propose(settings("deepseek"), e.request_context(), e.context())
    assert "secret" not in str(error.value)
    assert ledger.summary()["requests"][0]["status"] != "pending"


def test_restart_pending_and_atomic_quota(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    ledger = Ledger(str(tmp_path / "ledger.db"), limit=1)
    def reserve(_):
        try:
            return ledger.reserve("deepseek", "hash")
        except ProviderError:
            return None
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(reserve, range(4)))
    assert sum(x is not None for x in results) == 1
    restarted = Ledger(ledger.path, limit=1)
    assert restarted.summary()["requests"][0]["status"] == "abandoned_on_restart"
    assert restarted.summary()["remaining"] == 0


async def test_disabled_never_sends(tmp_path):
    service = ModelService(Ledger(str(tmp_path / "ledger.db")))
    e = Engine()
    with pytest.raises(ProviderError, match="disabled"):
        await service.propose(settings("deepseek"), e.request_context(), e.context())
    assert service.ledger.summary()["reserved_requests"] == 0


def test_configuration_and_usage_boundaries():
    s = settings("deepseek")
    with pytest.raises(ProviderError):
        build_request(replace(s, endpoint="http://untrusted.invalid"), {})
    with pytest.raises(ProviderError):
        build_request(replace(s, model=""), {})
    with pytest.raises(ProviderError):
        build_request(replace(s, api_key=""), {})
    with pytest.raises(ProviderError):
        build_request(s, {"x": "a" * 40000})
    assert usage_of("chat", {"usage": {"prompt_tokens": -1, "completion_tokens": True}})["known"] is False
    assert "fixture-secret" not in repr(s)

# Expectations are independent of the registry, taken from the linked official contracts.
@pytest.mark.parametrize('name,header,token_field', [
    ('mimo', 'api-key', 'max_completion_tokens'),
    ('doubao', 'authorization', 'max_completion_tokens'),
    ('deepseek', 'authorization', 'max_tokens'),
    ('openai-chat', 'authorization', 'max_completion_tokens'),
    ('groq', 'authorization', 'max_completion_tokens'),
    ('azure', 'api-key', 'max_completion_tokens'),
])
def test_documented_profile_differences(name, header, token_field):
    _, headers, body = build_request(settings(name), Engine().request_context())
    assert header in {k.lower() for k in headers}
    assert body[token_field] == 1024
    alternative = 'max_tokens' if token_field == 'max_completion_tokens' else 'max_completion_tokens'
    assert alternative not in body


@pytest.mark.parametrize('name', ['deepseek', 'openai', 'gemini', 'ollama'])
def test_opt_in_json_mode_has_protocol_specific_shape(name):
    _, _, body = build_request(replace(settings(name), json_mode=True), Engine().request_context())
    if name == 'deepseek':
        assert body['response_format'] == {'type': 'json_object'}
    elif name == 'openai':
        assert body['text'] == {'format': {'type': 'json_object'}}
    elif name == 'gemini':
        assert body['generationConfig']['responseMimeType'] == 'application/json'
    else:
        assert body['format'] == 'json'


@pytest.mark.parametrize('protocol,data', [
    ('chat', {'choices':[{'finish_reason':'stop','message':'wrong'}]}),
    ('responses', {'status':'completed','output':[{'type':'function_call'}]}),
    ('messages', {'stop_reason':'end_turn','content':[{'type':'tool_use'}]}),
    ('gemini', {'candidates':[{'finishReason':'STOP','content':{'parts':[{'functionCall':{}}]}}]}),
    ('ollama', {'done':True,'done_reason':'stop','message':{'tool_calls':[{}],'content':'{}'}}),
])
def test_malformed_and_tool_responses_are_fixed_errors(protocol, data):
    with pytest.raises(ProviderError, match='invalid_provider_response'):
        parse_response(protocol, data)


async def test_chunked_response_cannot_bypass_size_limit(tmp_path):
    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'x' * 70000
            yield b'x' * 70000
    async def handler(req):
        return httpx.Response(200, stream=Chunks())
    ledger = Ledger(str(tmp_path / 'ledger.db'))
    service = ModelService(ledger, enabled=True, transport=httpx.MockTransport(handler))
    e = Engine()
    with pytest.raises(ProviderError, match='response_too_large'):
        await service.propose(settings('deepseek'), e.request_context(), e.context())
    assert ledger.summary()['requests'][0]['status'] == 'response_too_large'
