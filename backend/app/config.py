from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    cookie_115: str = ""
    app_port: int = 8000
    debug: bool = False
    http_proxy: Optional[str] = None  # e.g. "http://127.0.0.1:7890"


settings = Settings()
