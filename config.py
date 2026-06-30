from pydantic_settings import BaseSettings
from pydantic import field_validator


class Settings(BaseSettings):
    bot_token: str
    database_url: str
    redis_url: str
    superadmin_ids: list[int] = []

    @field_validator("superadmin_ids", mode="before")
    @classmethod
    def parse_ids(cls, v: str | list) -> list[int]:
        if isinstance(v, str):
            return [int(i.strip()) for i in v.split(",") if i.strip()]
        if isinstance(v, int):
            return [v]
        if isinstance(v, list):
            return v
        return []

    model_config = {"env_file": ".env"}


settings = Settings()
