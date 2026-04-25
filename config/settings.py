from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = Field(..., description="Async PostgreSQL connection string")
    redis_url: str = "redis://localhost:6379"
    sec_user_agent: str = Field(..., description="AppName/1.0 email@domain.com")
    sec_base_url: str = "https://data.sec.gov"
    sec_requests_per_second: int = 8
    anthropic_api_key: Optional[str] = None
    openai_api_key: Optional[str] = None
    embedding_model: str = "all-MiniLM-L6-v2"
    sp500_only: bool = True
    max_workers: int = 4

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
