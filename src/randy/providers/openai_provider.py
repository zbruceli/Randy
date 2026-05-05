from typing import Literal

from openai import AsyncOpenAI

from .base import ProviderResponse
from .pricing import price_for

Api = Literal["chat", "responses"]


class OpenAIProvider:
    """OpenAI-compatible provider.

    `api="chat"` uses /v1/chat/completions (works for most chat-tuned models and
    OpenAI-compatible endpoints like DeepSeek).

    `api="responses"` uses /v1/responses (required for Pro reasoning models like
    gpt-5.5-pro that don't accept chat/completions).
    """

    name = "openai"

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str | None = None,
        api: Api = "chat",
    ):
        self.model = model
        self.api = api
        self._client = (
            AsyncOpenAI(api_key=api_key, base_url=base_url) if base_url else AsyncOpenAI(api_key=api_key)
        )

    async def complete(self, system: str, messages: list[dict], **kwargs) -> ProviderResponse:
        if self.api == "responses":
            return await self._complete_responses(system, messages, **kwargs)
        return await self._complete_chat(system, messages, **kwargs)

    async def _complete_chat(self, system: str, messages: list[dict], **kwargs) -> ProviderResponse:
        oai_messages = [{"role": "system", "content": system}, *messages]
        # GPT-5+ rejects `max_tokens`; use `max_completion_tokens` instead.
        params = {k: v for k, v in kwargs.items() if k in {"temperature", "top_p"}}
        if "max_tokens" in kwargs:
            params["max_completion_tokens"] = kwargs["max_tokens"]
        resp = await self._client.chat.completions.create(
            model=self.model,
            messages=oai_messages,
            **params,
        )
        msg = resp.choices[0].message
        text = msg.content or getattr(msg, "reasoning_content", "") or ""
        in_tok = resp.usage.prompt_tokens if resp.usage else 0
        out_tok = resp.usage.completion_tokens if resp.usage else 0
        return ProviderResponse(
            text=text,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=price_for(self.model).cost(in_tok, out_tok),
            model=self.model,
        )

    async def _complete_responses(self, system: str, messages: list[dict], **kwargs) -> ProviderResponse:
        max_out = kwargs.get("max_tokens") or kwargs.get("max_output_tokens") or 2048
        resp = await self._client.responses.create(
            model=self.model,
            instructions=system,
            input=messages,
            max_output_tokens=max_out,
        )
        text = getattr(resp, "output_text", "") or ""
        usage = getattr(resp, "usage", None)
        in_tok = getattr(usage, "input_tokens", 0) if usage else 0
        out_tok = getattr(usage, "output_tokens", 0) if usage else 0
        return ProviderResponse(
            text=text,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=price_for(self.model).cost(in_tok, out_tok),
            model=self.model,
        )
