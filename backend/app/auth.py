"""FastAPI dependency that verifies a Supabase-issued JWT.

Two verification paths, selected by `Settings.supabase_auth_mode`:

- "jwks" (default): fetch the project's signing keys from
  `${SUPABASE_URL}/auth/v1/.well-known/jwks.json` via `PyJWKClient` and
  verify the token's signature against the matching public key. This is
  the mode for Supabase projects on asymmetric (RS256/ES256) signing keys.
- "legacy": verify the token as HS256, signed with the project's shared
  secret (`SUPABASE_JWT_SECRET`). This is the mode for older Supabase
  projects still on the legacy shared-secret signing key.

Whichever path runs, the verified `sub` claim is the Supabase user id that
every other table's `user_id` column is scoped by.
"""

from __future__ import annotations

from functools import lru_cache

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient

from app.config import Settings

_bearer_scheme = HTTPBearer(auto_error=False)

_JWKS_ALGORITHMS = ["RS256", "ES256"]


def get_settings() -> Settings:
    return Settings()


@lru_cache(maxsize=1)
def _get_jwks_client(jwks_url: str) -> PyJWKClient:
    """Build (and cache) the PyJWKClient for a JWKS URL.

    PyJWKClient caches fetched signing keys internally, so reusing one
    client instance across requests avoids re-fetching the JWKS on every
    call.
    """
    return PyJWKClient(jwks_url)


def _decode_jwks(token: str, settings: Settings) -> dict:
    jwks_url = f"{settings.supabase_url}/auth/v1/.well-known/jwks.json"
    client = _get_jwks_client(jwks_url)
    signing_key = client.get_signing_key_from_jwt(token)
    return jwt.decode(
        token,
        signing_key.key,
        algorithms=_JWKS_ALGORITHMS,
        options={"verify_aud": False},
    )


def _decode_legacy(token: str, settings: Settings) -> dict:
    if not settings.supabase_jwt_secret:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="SUPABASE_JWT_SECRET is not configured for legacy auth mode",
        )
    return jwt.decode(
        token,
        settings.supabase_jwt_secret,
        algorithms=["HS256"],
        options={"verify_aud": False},
    )


def verify_token(token: str, settings: Settings) -> str:
    """Verify a Supabase JWT and return the user id (`sub` claim).

    Raises `HTTPException(401)` for any missing/malformed/invalid/expired
    token.
    """
    try:
        if settings.supabase_auth_mode == "legacy":
            payload = _decode_legacy(token, settings)
        else:
            payload = _decode_jwks(token, settings)
    except HTTPException:
        raise
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token has expired"
        ) from exc
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token"
        ) from exc
    except Exception as exc:
        # PyJWKClient can raise non-PyJWTError failures too (e.g. a malformed
        # token with no parseable header before signature verification even
        # starts), so treat any other decode-time failure as an invalid token.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token"
        ) from exc

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token missing subject claim"
        )
    return user_id


def get_current_user_id(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    settings: Settings = Depends(get_settings),
) -> str:
    """FastAPI dependency: extract and verify the bearer token, return `sub`."""
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token"
        )
    return verify_token(credentials.credentials, settings)
