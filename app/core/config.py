"""Application configuration loaded from environment variables."""

from pydantic_settings import BaseSettings
from typing import List


class Settings(BaseSettings):
    # Supabase
    supabase_url: str = ""
    supabase_key: str = ""
    supabase_service_key: str = ""

    # Anthropic
    anthropic_api_key: str = ""

    # Redis (optional)
    redis_url: str = ""

    # CORS
    cors_origins: List[str] = ["http://localhost:3000"]

    # Rate limits
    rate_limit_free: str = "10/day"
    rate_limit_pro: str = "200/day"

    # Dev mode
    dev_api_key: str = ""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
