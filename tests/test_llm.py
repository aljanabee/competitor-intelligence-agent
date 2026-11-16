"""LLM provider selection and the Azure OpenAI call shape (fake client, no network)."""
import asyncio
import json
import time
from types import SimpleNamespace

from app.config import Settings
from app.providers import llm as llm_mod
from app.providers.llm import LLM

SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}


class FakeCompletions:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    async def create(self, **kw):
        self.calls.append(kw)
        return self.reply


def _reply(args=None, content=None):
    tool_calls = [SimpleNamespace(function=SimpleNamespace(arguments=json.dumps(args)))] if args else None
    msg = SimpleNamespace(tool_calls=tool_calls, content=content)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="tool_calls")])


def _azure(monkeypatch, reply, **over):
    s = Settings(llm_provider="azure_openai", azure_openai_endpoint="https://res.openai.azure.com/",
                 azure_openai_deployment="gpt-mini", azure_openai_api_key="k", mock_providers=False, **over)
    llm = LLM(s)
    fake = FakeCompletions(reply)
    keys = []

    def client(self, api_key):
        keys.append(api_key)
        return SimpleNamespace(chat=SimpleNamespace(completions=fake))

    monkeypatch.setattr(llm_mod._AzureOpenAIBackend, "_client", client)
    return llm, fake, keys


def test_azure_openai_forced_function_call(monkeypatch):
    llm, fake, keys = _azure(monkeypatch, _reply({"summary": "ok"}))
    assert llm.enabled and llm._backend.base_url == "https://res.openai.azure.com/openai/v1/"
    out = asyncio.run(llm.structured(name="synth", instructions="Analyse", content={"a": 1}, schema=SCHEMA))
    assert out == {"summary": "ok"} and keys == ["k"]
    call = fake.calls[0]
    assert call["model"] == "gpt-mini"
    assert call["tool_choice"] == {"type": "function", "function": {"name": "synth"}}
    assert call["tools"][0]["function"]["parameters"] == SCHEMA
    assert "max_completion_tokens" in call and "Danish" in call["messages"][0]["content"]


def test_azure_openai_plain_json_fallback(monkeypatch):
    llm, _, _ = _azure(monkeypatch, _reply(content='{"summary": "plain"}'))
    assert asyncio.run(llm.structured(name="x", instructions="i", content="c", schema=SCHEMA)) == {"summary": "plain"}


def test_endpoint_with_v1_suffix_not_doubled(monkeypatch):
    llm, _, _ = _azure(monkeypatch, _reply({"summary": "ok"}))
    llm2 = LLM(llm.s.model_copy(update={"azure_openai_endpoint": "https://res.openai.azure.com/openai/v1/"}))
    assert llm2._backend.base_url == "https://res.openai.azure.com/openai/v1/"


def test_managed_identity_token_used_and_cached(monkeypatch):
    llm, _, keys = _azure(monkeypatch, _reply({"summary": "ok"}), )
    llm.s.azure_openai_api_key = ""
    calls = []

    class FakeCred:
        async def get_token(self, scope):
            calls.append(scope)
            return SimpleNamespace(token="entra-token", expires_on=time.time() + 3600)

    llm._backend._credential = FakeCred()
    for _ in range(2):
        asyncio.run(llm.structured(name="x", instructions="i", content="c", schema=SCHEMA))
    assert keys == ["entra-token", "entra-token"]
    assert calls == ["https://cognitiveservices.azure.com/.default"]  # fetched once, then cached


def test_missing_config_reports_reason():
    llm = LLM(Settings(llm_provider="azure_openai", mock_providers=False))
    assert not llm.enabled and "AZURE_OPENAI_ENDPOINT" in llm.unavailable_reason
    llm = LLM(Settings(llm_provider="anthropic", anthropic_api_key="", mock_providers=False))
    assert not llm.enabled and "ANTHROPIC_API_KEY" in llm.unavailable_reason
