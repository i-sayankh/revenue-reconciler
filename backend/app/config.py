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

    @property
    def allowed_origins_list(self) -> list[str]:
        """`ALLOWED_ORIGINS` as a list for `CORSMiddleware`.

        Splits the comma-separated env value (e.g.
        `"http://localhost:3000,https://<app>.vercel.app"`) into individual
        origins, trimming whitespace and dropping empty entries.
        """
        return [origin.strip() for origin in self.allowed_origins.split(",") if origin.strip()]
