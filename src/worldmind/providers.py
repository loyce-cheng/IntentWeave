"""Documented protocol profiles, not a claim of live provider/model certification."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from pydantic import Field

from .domain import Action, StrictModel, canonical


@dataclass(frozen=True)
class Profile:
    protocol: str
    endpoint: str
    auth: str = "bearer"
    token_field: str = "max_tokens"
    extra: dict = field(default_factory=dict)
    local: bool = False


# No default model names: exact IDs/regions/entitlements belong to the operator.
PROFILES = {
    "openai": Profile("responses", "https://api.openai.com/v1/responses",
                      token_field="max_output_tokens"),
    "openai-chat": Profile("chat", "https://api.openai.com/v1/chat/completions",
                           token_field="max_completion_tokens"),
    "deepseek": Profile("chat", "https://api.deepseek.com/chat/completions",
                        extra={"thinking": {"type": "disabled"}}),
    "mimo": Profile("chat", "https://api.xiaomimimo.com/v1/chat/completions"),
    "gemini": Profile("gemini", "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                      auth="google", token_field="maxOutputTokens"),
    "anthropic": Profile("messages", "https://api.anthropic.com/v1/messages", auth="anthropic"),
    "qwen": Profile("chat", "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"),
    "moonshot": Profile("chat", "https://api.moonshot.cn/v1/chat/completions"),
    "zhipu": Profile("chat", "https://open.bigmodel.cn/api/paas/v4/chat/completions"),
    "siliconflow": Profile("chat", "https://api.siliconflow.cn/v1/chat/completions"),
    "openrouter": Profile("chat", "https://openrouter.ai/api/v1/chat/completions",
                          extra={"provider": {"allow_fallbacks": False, "require_parameters": True}}),
    "groq": Profile("chat", "https://api.groq.com/openai/v1/chat/completions",
                    token_field="max_completion_tokens"),
    "mistral": Profile("chat", "https://api.mistral.ai/v1/chat/completions"),
    "xai": Profile("chat", "https://api.x.ai/v1/chat/completions"),
    "together": Profile("chat", "https://api.together.ai/v1/chat/completions"),
    # Azure resource/deployment/API version must be copied from the operator's resource.
    "azure": Profile("chat", "", auth="api-key", token_field="max_completion_tokens"),
    "ollama": Profile("ollama", "http://localhost:11434/api/chat", auth="none",
                      token_field="num_predict", local=True),
    "vllm": Profile("chat", "", local=True),
}


class ProviderError(Exception):
    """Only fixed error codes may cross this boundary. Never upstream body/headers."""


class Proposal(StrictModel):
    actions: list[Action] = Field(min_length=1, max_length=30)


def strict_json(text: str):
    def unique(pairs):
        d = {}
        for k, v in pairs:
            if k in d:
                raise ValueError("duplicate JSON field")
            d[k] = v
        return d
    def constant(_):
        raise ValueError("nonfinite JSON")
    return json.loads(text, object_pairs_hook=unique, parse_constant=constant)


def parse_proposal(text: str) -> Proposal:
    if not isinstance(text, str) or len(text.encode()) > 16384:
        raise ProviderError("invalid_output_size")
    try:
        return Proposal.model_validate(strict_json(text))
    except (ValueError, TypeError, RecursionError):
        raise ProviderError("invalid_proposal") from None


@dataclass(frozen=True)
class Settings:
    provider: str
    model: str
    endpoint: str
    api_key: str = field(repr=False)
    max_output: int = 1024
    timeout: float = 20.0
    max_response_bytes: int = 131072
    json_mode: bool = False
    workspace_id: str = ""

    @property
    def profile(self):
        return PROFILES[self.provider]

    def validate(self):
        if self.provider not in PROFILES:
            raise ProviderError("unknown_provider")
        if not self.model or len(self.model) > 200 or any(c in self.model for c in "\r\n?#{}"):
            raise ProviderError("model_required")
        url = urlsplit(self.endpoint.replace("{model}", self.model))
        if url.username or url.password or url.fragment or not url.hostname:
            raise ProviderError("invalid_endpoint")
        if url.scheme != "https" and not (
            self.profile.local and url.scheme == "http" and url.hostname in ("localhost", "127.0.0.1", "::1")
        ):
            raise ProviderError("https_required")
        # No browser-controlled endpoint. Env configuration is trusted operator input.
        if self.profile.auth != "none" and not self.api_key:
            raise ProviderError("credential_missing")
        if not 16 <= self.max_output <= 4096 or not 0 < self.timeout <= 60:
            raise ProviderError("invalid_limits")
        if not 1024 <= self.max_response_bytes <= 262144:
            raise ProviderError("invalid_limits")

    @classmethod
    def from_env(cls, provider):
        if provider not in PROFILES:
            raise ProviderError("unknown_provider")
        prefix = "WORLDMIND_" + provider.upper().replace("-", "_")
        settings = cls(provider, os.getenv(prefix + "_MODEL", ""),
                       os.getenv(prefix + "_ENDPOINT", PROFILES[provider].endpoint),
                       os.getenv(prefix + "_API_KEY", ""),
                       workspace_id=os.getenv(prefix + "_WORKSPACE_ID", ""))
        settings.validate()
        return settings


def build_request(settings: Settings, context: dict) -> tuple[str, dict, dict]:
    settings.validate()
    p = settings.profile
    system = ('Return ONLY a JSON object {"actions":[{"kind":"move","target":"warehouse"}]}. '
              "Use only the supplied actor knowledge, never invent observations. "
              "Return a complete valid plan for the selected goal, or an inspect-ended observation prefix. "
              "No tools, explanation, markdown or additional fields. The example is syntax, not a solution.")
    user = canonical(context)
    if len(user.encode()) > 32768:
        raise ProviderError("context_too_large")
    headers = {"Content-Type": "application/json"}
    if p.auth == "bearer":
        headers["Authorization"] = "Bearer " + settings.api_key
    elif p.auth == "anthropic":
        headers.update({"x-api-key": settings.api_key, "anthropic-version": "2023-06-01"})
        if settings.workspace_id:
            headers["anthropic-workspace-id"] = settings.workspace_id
    elif p.auth == "google":
        headers["x-goog-api-key"] = settings.api_key
    elif p.auth == "api-key":
        headers["api-key"] = settings.api_key
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    body = {"model": settings.model, "stream": False}
    if p.protocol == "chat":
        body.update(messages=messages, **{p.token_field: settings.max_output}, **p.extra)
        if settings.json_mode:
            body["response_format"] = {"type": "json_object"}
    elif p.protocol == "responses":
        body.update(input=messages, max_output_tokens=settings.max_output, store=False)
        if settings.json_mode:
            body["text"] = {"format": {"type": "json_object"}}
    elif p.protocol == "messages":
        body.update(system=system, messages=messages[1:], max_tokens=settings.max_output)
        if settings.json_mode:
            raise ProviderError("json_mode_not_configured")
    elif p.protocol == "gemini":
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {"maxOutputTokens": settings.max_output}}
        if settings.json_mode:
            body["generationConfig"]["responseMimeType"] = "application/json"
    elif p.protocol == "ollama":
        body.update(messages=messages, options={"num_predict": settings.max_output})
        if settings.json_mode:
            body["format"] = "json"
    return settings.endpoint.replace("{model}", settings.model), headers, body


def usage_of(protocol: str, data: dict) -> dict:
    """Preserve absence as null, never call missing usage zero or infer a price."""
    source = data.get("usageMetadata") if protocol == "gemini" else data if protocol == "ollama" else data.get("usage")
    source = source if isinstance(source, dict) else {}
    keys = {
        "chat": ("prompt_tokens", "completion_tokens"),
        "responses": ("input_tokens", "output_tokens"),
        "messages": ("input_tokens", "output_tokens"),
        "gemini": ("promptTokenCount", "candidatesTokenCount"),
        "ollama": ("prompt_eval_count", "eval_count"),
    }[protocol]
    def number(value):
        return value if type(value) is int and 0 <= value <= 10**9 else None
    detail_keys = ("cache_creation_input_tokens", "cache_read_input_tokens", "prompt_cache_hit_tokens",
                   "prompt_cache_miss_tokens", "thoughtsTokenCount", "cachedContentTokenCount",
                   "totalTokenCount", "total_tokens", "prompt_eval_cached_count")
    details = {k: number(source[k]) for k in detail_keys if k in source}
    for k in ("prompt_tokens_details", "completion_tokens_details", "input_tokens_details", "output_tokens_details"):
        if isinstance(source.get(k), dict):
            details[k] = {n: number(v) for n, v in source[k].items() if n in ("cached_tokens", "reasoning_tokens")}
    i, o = (number(source.get(k)) for k in keys)
    return {"input_tokens": i, "output_tokens": o, "details": details,
            "known": i is not None and o is not None, "cost": None}


def parse_response(protocol: str, data: dict) -> tuple[Proposal, dict]:
    try:
        if not isinstance(data, dict) or data.get("error"):
            raise ValueError
        if protocol == "chat":
            choices = data["choices"]
            if len(choices) != 1 or choices[0]["finish_reason"] != "stop":
                raise ValueError
            msg = choices[0]["message"]
            if msg.get("tool_calls") or msg.get("refusal"):
                raise ValueError
            text = msg["content"]
        elif protocol == "responses":
            if data["status"] != "completed":
                raise ValueError
            parts = []
            for item in data["output"]:
                if item["type"] == "reasoning":
                    continue  # Never expose or save internal reasoning.
                if item["type"] != "message":
                    raise ValueError
                for part in item["content"]:
                    if part["type"] != "output_text":
                        raise ValueError
                    parts.append(part["text"])
            text = "".join(parts)
        elif protocol == "messages":
            if data["stop_reason"] != "end_turn":
                raise ValueError
            parts = data["content"]
            if any(x["type"] not in ("text", "thinking", "redacted_thinking") for x in parts):
                raise ValueError
            text = "".join(x["text"] for x in parts if x["type"] == "text")
        elif protocol == "gemini":
            cand = data["candidates"]
            if len(cand) != 1 or cand[0]["finishReason"] != "STOP":
                raise ValueError
            parts = cand[0]["content"]["parts"]
            if any("text" not in x for x in parts):
                raise ValueError
            text = "".join(x["text"] for x in parts if not x.get("thought"))
        else:
            if data["done"] is not True or data.get("done_reason") != "stop":
                raise ValueError
            if data["message"].get("tool_calls"):
                raise ValueError
            text = data["message"]["content"]
        proposal = parse_proposal(text)
        return proposal, usage_of(protocol, data)
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        raise ProviderError("invalid_provider_response") from None
