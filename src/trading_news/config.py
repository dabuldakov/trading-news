from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql://news:news@127.0.0.1:5433/news"
    service_key: str = ""

    main_api_url: str = ""
    main_service_key: str = ""

    moex_iss_base: str = "https://iss.moex.com/iss"
    moex_board: str = "TQBR"
    http_timeout: float = 20.0
    tz: str = "Europe/Moscow"

    news_interval_sec: int = 3600
    reference_cron_hour: int = 7
    reference_cron_minute: int = 30
    migrations_dir: str = "migrations"

    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
