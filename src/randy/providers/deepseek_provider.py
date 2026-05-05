from .openai_provider import OpenAIProvider


class DeepSeekProvider(OpenAIProvider):
    """DeepSeek exposes an OpenAI-compatible API; reuse the OpenAI client."""

    name = "deepseek"

    def __init__(self, api_key: str, model: str):
        super().__init__(api_key=api_key, model=model, base_url="https://api.deepseek.com/v1")
