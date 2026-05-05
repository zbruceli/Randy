from dataclasses import dataclass
from typing import Protocol


@dataclass
class ProviderResponse:
    text: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    model: str


class Provider(Protocol):
    name: str
    model: str

    async def complete(self, system: str, messages: list[dict], **kwargs) -> ProviderResponse: ...
