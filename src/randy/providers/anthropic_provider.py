from typing import cast

from anthropic import AsyncAnthropic
from anthropic.types import MessageParam

from .base import ProviderResponse
from .pricing import price_for


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str, model: str, max_tokens: int = 2048):
        self.model = model
        self.max_tokens = max_tokens
        self._client = AsyncAnthropic(api_key=api_key)

    async def complete(self, system: str, messages: list[dict], **kwargs) -> ProviderResponse:
        resp = await self._client.messages.create(
            model=self.model,
            system=system,
            messages=cast(list[MessageParam], messages),
            max_tokens=kwargs.get("max_tokens", self.max_tokens),
        )
        text = "".join(getattr(b, "text", "") for b in resp.content)
        in_tok = resp.usage.input_tokens
        out_tok = resp.usage.output_tokens
        return ProviderResponse(
            text=text,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=price_for(self.model).cost(in_tok, out_tok),
            model=self.model,
        )
