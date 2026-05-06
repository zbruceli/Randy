import logging
from typing import cast

from anthropic import AsyncAnthropic
from anthropic.types import MessageParam

from .base import ProviderResponse
from .pricing import price_for

logger = logging.getLogger("randy.providers.anthropic")


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str, model: str, max_tokens: int = 2048):
        self.model = model
        self.max_tokens = max_tokens
        self._client = AsyncAnthropic(api_key=api_key)

    async def complete(self, system: str, messages: list[dict], **kwargs) -> ProviderResponse:
        # Cache the system prompt — persona text (~2-4 KB) is reused across rounds and sessions.
        # Anthropic will only honor the cache if the block is ≥1024 tokens; below that, no harm.
        system_blocks = [
            {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
        ]
        resp = await self._client.messages.create(
            model=self.model,
            system=cast(list, system_blocks),
            messages=cast(list[MessageParam], messages),
            max_tokens=kwargs.get("max_tokens", self.max_tokens),
        )
        text = "".join(getattr(b, "text", "") for b in resp.content)

        in_tok = resp.usage.input_tokens
        out_tok = resp.usage.output_tokens
        cache_create = getattr(resp.usage, "cache_creation_input_tokens", 0) or 0
        cache_read = getattr(resp.usage, "cache_read_input_tokens", 0) or 0

        cost = price_for(self.model).cost_with_cache(
            input_tokens=in_tok,
            output_tokens=out_tok,
            cache_create=cache_create,
            cache_read=cache_read,
        )

        if cache_read or cache_create:
            logger.info(
                "anthropic cache: read=%d create=%d fresh_in=%d (saved ~%.0f%% on cached part)",
                cache_read,
                cache_create,
                in_tok,
                90.0 if cache_read else 0.0,
            )

        return ProviderResponse(
            text=text,
            input_tokens=in_tok + cache_create + cache_read,
            output_tokens=out_tok,
            cost_usd=cost,
            model=self.model,
        )
