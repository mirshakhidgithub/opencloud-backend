"""
Server-side token vault (spec §5.2, §9.1).

The raw Zadara token is stored ONLY here — in the Django cache (Redis in
prod), encrypted at rest with Fernet, keyed by the user's session, with a TTL
shorter than Zadara's ~4h token life. It is never sent to the browser.
"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.cache import cache

# Kept a bit under Zadara's ~4h token lifetime, and shared with SESSION_COOKIE_AGE
# so the session and the token it depends on expire together.
DEFAULT_TTL_SECONDS = 3 * 60 * 60

_KEY_PREFIX = 'zadara_token:'


def fernet() -> Fernet:
    key = settings.TOKEN_VAULT_KEY
    if key:
        return Fernet(key.encode() if isinstance(key, str) else key)

    # Dev fallback: derive a stable key from SECRET_KEY. Set TOKEN_VAULT_KEY in prod.
    digest = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def ttl_seconds() -> int:
    return getattr(settings, 'ZADARA_TOKEN_TTL', DEFAULT_TTL_SECONDS)


def store(session_key: str, token: str, ttl: int | None = None) -> None:
    encrypted = fernet().encrypt(token.encode()).decode()
    cache.set(f'{_KEY_PREFIX}{session_key}', encrypted, timeout=ttl or ttl_seconds())


def get(session_key: str) -> str | None:
    encrypted = cache.get(f'{_KEY_PREFIX}{session_key}')
    if not encrypted:
        return None
    try:
        return fernet().decrypt(encrypted.encode()).decode()
    except InvalidToken:
        return None


def delete(session_key: str) -> None:
    cache.delete(f'{_KEY_PREFIX}{session_key}')
