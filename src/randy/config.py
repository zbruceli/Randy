from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    anthropic_api_key: str = ""
    openai_api_key: str = ""
    google_api_key: str = ""
    deepseek_api_key: str = ""

    telegram_bot_token: str = ""
    telegram_allowed_user_ids: str = ""

    db_path: str = "./randy.sqlite"
    session_cost_cap_usd: float = 25.0
    per_model_cost_cap_usd: float = 2.0

    facilitator_model: str = "gemini-3-pro-preview"
    expert_anthropic_model: str = "claude-sonnet-4-6"
    expert_openai_model: str = "gpt-5.5"
    expert_deepseek_model: str = "deepseek-v4-pro"


settings = Settings()
