"""
The short-lived ticket between step 1 (password) and step 2 (TOTP) of a sign-in.

Same shape as `authentication/mfa.py`, and for the same reason: step 1 must not
leave the browser holding anything reusable. The ticket is an opaque id; what it
stands for — which operator, and whether this is an enrolment — lives in the
cache and expires on its own.
"""

import secrets

from django.core.cache import cache

TTL_SECONDS = 4 * 60

_KEY_PREFIX = 'platform_login_ticket:'


def issue(admin_id: int, *, enrolling: bool, pending_secret_encrypted: str | None = None) -> str:
    """`pending_secret_encrypted` is an enrolling operator's not-yet-saved TOTP
    secret, already through `totp.encrypt` — the cache is Redis, and a secret
    that survives a `KEYS *` for four minutes is still a secret that leaked."""
    ticket = secrets.token_urlsafe(24)
    cache.set(
        f'{_KEY_PREFIX}{ticket}',
        {'admin_id': admin_id, 'enrolling': enrolling, 'pending_secret': pending_secret_encrypted},
        timeout=TTL_SECONDS,
    )
    return ticket


def load(ticket: str) -> dict | None:
    """Non-destructive: a mistyped code can be retried while the ticket lives."""
    if not ticket:
        return None
    return cache.get(f'{_KEY_PREFIX}{ticket}')


def revoke(ticket: str) -> None:
    cache.delete(f'{_KEY_PREFIX}{ticket}')
