"""Unit tests for provider adapters using mocked SDK clients.

We don't hit real APIs here. We verify:
  - response parsing extracts text + token counts correctly
  - cost calculation uses the pricing table for the configured model
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from randy.providers.anthropic_provider import AnthropicProvider
from randy.providers.deepseek_provider import DeepSeekProvider
from randy.providers.google_provider import GoogleProvider
from randy.providers.openai_provider import OpenAIProvider


@pytest.mark.asyncio
async def test_anthropic_parses_response():
    p = AnthropicProvider(api_key="x", model="claude-opus-4-7")
    fake = SimpleNamespace(
        content=[SimpleNamespace(text="hello world", type="text")],
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )
    p._client.messages.create = AsyncMock(return_value=fake)

    resp = await p.complete(system="s", messages=[{"role": "user", "content": "hi"}])

    assert resp.text == "hello world"
    assert resp.input_tokens == 10
    assert resp.output_tokens == 5
    assert resp.model == "claude-opus-4-7"
    assert resp.cost_usd > 0


@pytest.mark.asyncio
async def test_openai_parses_response():
    p = OpenAIProvider(api_key="x", model="gpt-5.2-pro")
    fake = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
        usage=SimpleNamespace(prompt_tokens=20, completion_tokens=4),
    )
    p._client.chat.completions.create = AsyncMock(return_value=fake)

    resp = await p.complete(system="s", messages=[{"role": "user", "content": "hi"}])

    assert resp.text == "ok"
    assert resp.input_tokens == 20
    assert resp.output_tokens == 4
    assert resp.cost_usd > 0


@pytest.mark.asyncio
async def test_google_parses_response():
    p = GoogleProvider(api_key="x", model="gemini-3-pro")
    fake = SimpleNamespace(
        text="answer",
        usage_metadata=SimpleNamespace(prompt_token_count=15, candidates_token_count=8),
    )
    aio_models = MagicMock()
    aio_models.generate_content = AsyncMock(return_value=fake)
    p._client = SimpleNamespace(aio=SimpleNamespace(models=aio_models))

    resp = await p.complete(system="s", messages=[{"role": "user", "content": "hi"}])

    assert resp.text == "answer"
    assert resp.input_tokens == 15
    assert resp.output_tokens == 8
    assert resp.cost_usd > 0


@pytest.mark.asyncio
async def test_deepseek_uses_openai_compatible_path():
    p = DeepSeekProvider(api_key="x", model="deepseek-v3.2-speciale")
    fake = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="ds"))],
        usage=SimpleNamespace(prompt_tokens=7, completion_tokens=2),
    )
    p._client.chat.completions.create = AsyncMock(return_value=fake)

    resp = await p.complete(system="s", messages=[{"role": "user", "content": "hi"}])

    assert resp.text == "ds"
    assert resp.model == "deepseek-v3.2-speciale"
    assert p.name == "deepseek"
