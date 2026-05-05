from typing import Any, cast

from google import genai
from google.genai import types

from .base import ProviderResponse
from .pricing import price_for


class GoogleProvider:
    name = "google"

    def __init__(self, api_key: str, model: str):
        self.model = model
        self._client = genai.Client(api_key=api_key)

    async def complete(self, system: str, messages: list[dict], **kwargs) -> ProviderResponse:
        contents = [
            types.Content(
                role="user" if m["role"] == "user" else "model",
                parts=[types.Part(text=m["content"])],
            )
            for m in messages
        ]
        resp = await self._client.aio.models.generate_content(
            model=self.model,
            contents=cast(Any, contents),
            config=types.GenerateContentConfig(
                system_instruction=system,
                max_output_tokens=kwargs.get("max_tokens", 2048),
            ),
        )
        text = resp.text or ""
        usage = resp.usage_metadata
        in_tok = getattr(usage, "prompt_token_count", 0) or 0
        out_tok = getattr(usage, "candidates_token_count", 0) or 0
        return ProviderResponse(
            text=text,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=price_for(self.model).cost(in_tok, out_tok),
            model=self.model,
        )
