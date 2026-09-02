"""Tests for backend/app/auth.py that don't require a live Supabase project.

`verify_token`/`get_current_user_id` are exercised directly with
self-signed test tokens:

- legacy (HS256) path: signed with a throwaway secret, matching
  `Settings.supabase_auth_mode == "legacy"` -- no network involved.
- jwks path: signed with a locally generated RSA key, with
  `auth._get_jwks_client` monkeypatched to return the matching public key
  instead of fetching a real JWKS document.

This proves the dependency's logic (valid token -> user id extracted;
expired token -> 401; garbage token -> 401; missing header -> 401)
without depending on network access or a real Supabase project. The live
acceptance check (a real Supabase-issued JWT) is separate and requires
SUPABASE_URL/a real access token.
"""

import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app import auth
from app.config import Settings

TEST_JWT_SECRET = "throwaway-test-secret-at-least-32-bytes-long"


def make_settings(**overrides) -> Settings:
    defaults = dict(
        database_url="postgresql://fake-user:fake-pass@localhost/fake",
        supabase_url="https://fake.supabase.co",
        supabase_jwt_secret=TEST_JWT_SECRET,
        supabase_auth_mode="legacy",
        groq_api_key="fake-key",
        allowed_origins="http://localhost:3000",
    )
    defaults.update(overrides)
    return Settings(**defaults)


def make_legacy_token(sub: str, *, exp_delta: timedelta, secret: str = TEST_JWT_SECRET) -> str:
    now = datetime.now(timezone.utc)
    payload = {"sub": sub, "iat": now, "exp": now + exp_delta}
    return jwt.encode(payload, secret, algorithm="HS256")


# -- legacy (HS256) path -----------------------------------------------------


def test_verify_token_legacy_valid_token_returns_user_id():
    user_id = str(uuid.uuid4())
    settings = make_settings()
    token = make_legacy_token(user_id, exp_delta=timedelta(hours=1))

    assert auth.verify_token(token, settings) == user_id


def test_verify_token_legacy_expired_token_raises_401():
    settings = make_settings()
    token = make_legacy_token(str(uuid.uuid4()), exp_delta=timedelta(hours=-1))

    with pytest.raises(HTTPException) as exc_info:
        auth.verify_token(token, settings)

    assert exc_info.value.status_code == 401


def test_verify_token_legacy_wrong_secret_raises_401():
    settings = make_settings()
    token = make_legacy_token(
        str(uuid.uuid4()), exp_delta=timedelta(hours=1), secret="a-completely-different-wrong-secret-value"
    )

    with pytest.raises(HTTPException) as exc_info:
        auth.verify_token(token, settings)

    assert exc_info.value.status_code == 401


def test_verify_token_garbage_token_raises_401():
    settings = make_settings()

    with pytest.raises(HTTPException) as exc_info:
        auth.verify_token("this-is-not-a-jwt", settings)

    assert exc_info.value.status_code == 401


def test_verify_token_missing_sub_claim_raises_401():
    settings = make_settings()
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {"iat": now, "exp": now + timedelta(hours=1)}, TEST_JWT_SECRET, algorithm="HS256"
    )

    with pytest.raises(HTTPException) as exc_info:
        auth.verify_token(token, settings)

    assert exc_info.value.status_code == 401


def test_verify_token_legacy_missing_secret_raises_500():
    settings = make_settings(supabase_jwt_secret=None)
    token = make_legacy_token(str(uuid.uuid4()), exp_delta=timedelta(hours=1))

    with pytest.raises(HTTPException) as exc_info:
        auth.verify_token(token, settings)

    assert exc_info.value.status_code == 500


# -- jwks path (mocked client, no network) -----------------------------------


def test_verify_token_jwks_valid_token_returns_user_id(monkeypatch):
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()

    user_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {"sub": user_id, "iat": now, "exp": now + timedelta(hours=1)},
        private_key,
        algorithm="RS256",
    )

    class _FakeSigningKey:
        key = public_key

    class _FakeJwksClient:
        def get_signing_key_from_jwt(self, token):
            return _FakeSigningKey()

    monkeypatch.setattr(auth, "_get_jwks_client", lambda jwks_url: _FakeJwksClient())

    settings = make_settings(supabase_auth_mode="jwks", supabase_jwt_secret=None)

    assert auth.verify_token(token, settings) == user_id


def test_verify_token_jwks_expired_token_raises_401(monkeypatch):
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()

    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {"sub": str(uuid.uuid4()), "iat": now - timedelta(hours=2), "exp": now - timedelta(hours=1)},
        private_key,
        algorithm="RS256",
    )

    class _FakeSigningKey:
        key = public_key

    class _FakeJwksClient:
        def get_signing_key_from_jwt(self, token):
            return _FakeSigningKey()

    monkeypatch.setattr(auth, "_get_jwks_client", lambda jwks_url: _FakeJwksClient())

    settings = make_settings(supabase_auth_mode="jwks", supabase_jwt_secret=None)

    with pytest.raises(HTTPException) as exc_info:
        auth.verify_token(token, settings)

    assert exc_info.value.status_code == 401


# -- get_current_user_id dependency (missing/garbage header) -----------------


def test_get_current_user_id_missing_header_raises_401():
    settings = make_settings()

    with pytest.raises(HTTPException) as exc_info:
        auth.get_current_user_id(credentials=None, settings=settings)

    assert exc_info.value.status_code == 401


def test_get_current_user_id_valid_credentials_returns_user_id():
    user_id = str(uuid.uuid4())
    settings = make_settings()
    token = make_legacy_token(user_id, exp_delta=timedelta(hours=1))
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

    assert auth.get_current_user_id(credentials=credentials, settings=settings) == user_id
