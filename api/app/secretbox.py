"""Encrypt connector secrets (API keys) before they are stored in the database.

Key = derived from SECRET_KEY. If you change SECRET_KEY, saved connector secrets can no longer be read and
must be entered again (the app tells you; it never crashes).
"""
from __future__ import annotations

import base64
import hashlib
import json

from cryptography.fernet import Fernet, InvalidToken


def _fernet(secret_key: str) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(("cheapedeal-v1|" + secret_key).encode()).digest()))


def seal(secret_key: str, data: dict) -> str:
    return _fernet(secret_key).encrypt(json.dumps(data).encode()).decode() if data else ""


def unseal(secret_key: str, token: str) -> dict:
    if not token:
        return {}
    try:
        return json.loads(_fernet(secret_key).decrypt(token.encode()))
    except (InvalidToken, ValueError):
        return {}
