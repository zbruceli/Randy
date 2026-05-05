"""Per-model pricing in USD per million tokens.

Approximate published rates as of 2026-05; tune via env or here as vendors update.
Cache hits are billed differently — we ignore that here and bill at full input rate
(conservative — actual cost will be lower than estimated).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Price:
    input_per_mtok: float
    output_per_mtok: float

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return (
            input_tokens / 1_000_000 * self.input_per_mtok
            + output_tokens / 1_000_000 * self.output_per_mtok
        )


_DEFAULT = Price(input_per_mtok=10.0, output_per_mtok=30.0)

PRICES: dict[str, Price] = {
    # Anthropic Opus tier
    "claude-opus-4-7": Price(15.0, 75.0),
    "claude-opus-4-7[1m]": Price(15.0, 75.0),
    "claude-opus-4-5": Price(15.0, 75.0),
    # Anthropic Sonnet tier
    "claude-sonnet-4-6": Price(3.0, 15.0),
    # OpenAI Pro tier
    "gpt-5-pro": Price(15.0, 60.0),
    "gpt-5.2-pro": Price(15.0, 60.0),
    "gpt-5.5-pro": Price(15.0, 60.0),
    "gpt-5.5": Price(2.5, 10.0),
    # Google Gemini Pro tier
    "gemini-3-pro": Price(3.5, 10.5),
    "gemini-3-pro-preview": Price(3.5, 10.5),
    "gemini-3.1-pro-preview": Price(3.5, 10.5),
    "gemini-2.5-pro": Price(1.25, 10.0),
    # Google Gemini Flash tier (~10× cheaper than Pro)
    "gemini-flash-latest": Price(0.30, 2.50),
    "gemini-3-flash-preview": Price(0.30, 2.50),
    "gemini-2.5-flash": Price(0.075, 0.30),
    "gemini-2.5-flash-lite": Price(0.04, 0.15),
    "gemini-flash-lite-latest": Price(0.10, 0.40),
    # DeepSeek
    "deepseek-v3.2-speciale": Price(0.27, 1.10),
    "deepseek-v4-pro": Price(0.27, 1.10),
    "deepseek-v4-flash": Price(0.10, 0.50),
    "deepseek-chat": Price(0.27, 1.10),
}


def price_for(model: str) -> Price:
    return PRICES.get(model, _DEFAULT)
