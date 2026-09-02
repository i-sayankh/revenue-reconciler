from typing import Literal

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str
    supabase_url: str
    supabase_jwt_secret: str | None = None
    supabase_auth_mode: Literal["jwks", "legacy"] = "jwks"
    groq_api_key: str
    allowed_origins: str

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
