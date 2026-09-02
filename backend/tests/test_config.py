"""Tests for backend/app/config.py's `allowed_origins_list` helper.

`ALLOWED_ORIGINS` is stored as a single comma-separated env string (see
`.env.example`); `Settings.allowed_origins_list` is what `app.main` feeds
to `CORSMiddleware`, so its parsing (splitting, trimming, dropping empty
entries) is exercised directly here without needing the app or a live env
file.
"""

from __future__ import annotations

from app.config import Settings


def _settings(allowed_origins: str) -> Settings:
    return Settings(
        database_url="postgresql://fake-user:fake-pass@localhost/fake",
        supabase_url="https://fake.supabase.co",
        supabase_jwt_secret=None,
        groq_api_key="fake-key",
        allowed_origins=allowed_origins,
    )


def test_allowed_origins_list_single_origin():
    assert _settings("http://localhost:3000").allowed_origins_list == ["http://localhost:3000"]


def test_allowed_origins_list_splits_comma_separated_values():
    settings = _settings("http://localhost:3000,https://app.vercel.app")
    assert settings.allowed_origins_list == ["http://localhost:3000", "https://app.vercel.app"]


def test_allowed_origins_list_trims_whitespace_and_drops_empty_entries():
    settings = _settings(" http://localhost:3000 , , https://app.vercel.app ")
    assert settings.allowed_origins_list == ["http://localhost:3000", "https://app.vercel.app"]


def test_allowed_origins_list_empty_string_yields_empty_list():
    assert _settings("").allowed_origins_list == []
