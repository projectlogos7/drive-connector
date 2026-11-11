"""Secrets: Fernet encryption for OAuth tokens, hashing for API keys, signed state."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

from cryptography.fernet import Fernet

from .config import get_settings


def _fernet() -> Fernet:
    key = get_settings().encryption_key
    if not key:
        # Derive a stable key from SECRET_KEY so development works; production must set ENCRYPTION_KEY.
        key = base64.urlsafe_b64encode(hashlib.sha256(get_settings().secret_key.encode()).digest()).decode()
    return Fernet(key.encode())


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()


def new_key(prefix: str) -> str:
    """Return a new random API key like ``nd_admin_<random>``."""
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def sign_state(data: dict, ttl_seconds: int = 900) -> str:
    payload = dict(data, exp=int(time.time()) + ttl_seconds)
    raw = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
    sig = hmac.new(get_settings().secret_key.encode(), raw.encode(), hashlib.sha256).hexdigest()
    return f"{raw}.{sig}"


def verify_state(state: str) -> dict:
    try:
        raw, sig = state.rsplit(".", 1)
    except ValueError as e:
        raise ValueError("malformed state") from e
    expect = hmac.new(get_settings().secret_key.encode(), raw.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expect):
        raise ValueError("bad state signature")
    data = json.loads(base64.urlsafe_b64decode(raw.encode()))
    if data.get("exp", 0) < time.time():
        raise ValueError("state expired")
    return data
