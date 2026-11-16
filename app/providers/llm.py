"""LLM wrapper that always returns schema-shaped JSON via forced function/tool calling.

Providers (LLM_PROVIDER):
* azure_openai - Azure OpenAI v1 API (https://<resource>.openai.azure.com/openai/v1/).
                 Auth with AZURE_OPENAI_API_KEY, or managed identity / Entra ID when no key is set.
* anthropic    - Claude API (ANTHROPIC_API_KEY).
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from app.config import Settings

log = logging.getLogger(__name__)
_SCOPE = "https://cognitiveservices.azure.com/.default"


class LLMUnavailable(RuntimeError):
    pass


def _placeholder(schema: dict) -> Any:
    """Schema-shaped dummy value - used in mock mode."""
    t = schema.get("type")
    if "enum" in schema:
        return schema["enum"][0]
    if t == "object":
        return {k: _placeholder(v) for k, v in schema.get("properties", {}).items()}
    if t == "array":
        return [_placeholder(schema.get("items", {"type": "string"}))]
    if t in ("number", "integer"):
        return 0
    if t == "boolean":
        return False
    return "[mock] " + schema.get("description", "text")[:60]


def _system_prompt(lang: str) -> str:
    lang_name = {"da": "Danish", "en": "English"}.get(lang, lang)
    return ("You are a senior competitive-intelligence analyst. Base every statement strictly on "
            "the data provided; if the data is thin, say so instead of guessing. Quote short "
            "evidence snippets where useful. Write all free-text values in " + lang_name + ".")


def _user_prompt(instructions: str, content: Any) -> str:
    payload = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, default=str)
    return f"{instructions}\n\n<data>\n{payload[:180_000]}\n</data>"


class _AzureOpenAIBackend:
    def __init__(self, s: Settings):
        from openai import AsyncOpenAI  # noqa: F401 - fail fast if missing

        if not (s.azure_openai_endpoint and s.azure_openai_deployment):
            raise LLMUnavailable("AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_DEPLOYMENT must be set")
        self.s = s
        self.base_url = s.azure_openai_endpoint.rstrip("/").removesuffix("/openai/v1") + "/openai/v1/"
        self._credential = None
        self._token: tuple[str, float] | None = None

    async def _api_key(self) -> str:
        if self.s.azure_openai_api_key:
            return self.s.azure_openai_api_key
        # Managed identity / Entra ID - token cached until 5 min before expiry
        if self._token and self._token[1] - 300 > time.time():
            return self._token[0]
        from azure.identity.aio import DefaultAzureCredential

        if self._credential is None:
            self._credential = DefaultAzureCredential()
        tok = await self._credential.get_token(_SCOPE)
        self._token = (tok.token, tok.expires_on)
        return tok.token

    def _client(self, api_key: str):
        from openai import AsyncOpenAI

        return AsyncOpenAI(base_url=self.base_url, api_key=api_key, timeout=300, max_retries=3)

    async def call(self, name: str, instructions: str, system: str, user: str, schema: dict) -> dict:
        client = self._client(await self._api_key())
        resp = await client.chat.completions.create(
            model=self.s.azure_openai_deployment,  # Azure: the deployment name
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            tools=[{"type": "function", "function": {
                "name": name, "description": instructions[:1000], "parameters": schema}}],
            tool_choice={"type": "function", "function": {"name": name}},
            max_completion_tokens=self.s.llm_max_tokens,
        )
        msg = resp.choices[0].message
        if msg.tool_calls:
            return json.loads(msg.tool_calls[0].function.arguments)
        if msg.content:  # some models answer in plain JSON despite tool_choice
            return json.loads(msg.content)
        raise RuntimeError(f"model returned no structured output (finish_reason={resp.choices[0].finish_reason})")


class _AnthropicBackend:
    def __init__(self, s: Settings):
        if not s.anthropic_api_key:
            raise LLMUnavailable("ANTHROPIC_API_KEY not configured")
        import anthropic

        self.s = s
        self.client = anthropic.AsyncAnthropic(api_key=s.anthropic_api_key)

    async def call(self, name: str, instructions: str, system: str, user: str, schema: dict) -> dict:
        msg = await self.client.messages.create(
            model=self.s.llm_model, max_tokens=self.s.llm_max_tokens, system=system,
            tools=[{"name": name, "description": instructions, "input_schema": schema}],
            tool_choice={"type": "tool", "name": name},
            messages=[{"role": "user", "content": user}],
        )
        for block in msg.content:
            if block.type == "tool_use":
                return block.input
        raise RuntimeError("model returned no structured output")


class LLM:
    def __init__(self, settings: Settings):
        self.s = settings
        self.provider = settings.llm_provider
        self._backend = None
        self.unavailable_reason: str | None = None
        if settings.mock_providers:
            return
        try:
            if settings.llm_provider == "azure_openai":
                self._backend = _AzureOpenAIBackend(settings)
            elif settings.llm_provider == "anthropic":
                self._backend = _AnthropicBackend(settings)
            else:
                raise LLMUnavailable(f"unknown LLM_PROVIDER '{settings.llm_provider}'")
        except LLMUnavailable as e:
            self.unavailable_reason = str(e)

    @property
    def enabled(self) -> bool:
        return self._backend is not None or self.s.mock_providers

    async def structured(self, *, name: str, instructions: str, content: Any,
                         schema: dict, language: str | None = None) -> dict:
        if self.s.mock_providers:
            return _placeholder(schema)
        if self._backend is None:
            raise LLMUnavailable(self.unavailable_reason or "LLM not configured")
        return await self._backend.call(name, instructions, _system_prompt(language or self.s.report_language),
                                        _user_prompt(instructions, content), schema)
