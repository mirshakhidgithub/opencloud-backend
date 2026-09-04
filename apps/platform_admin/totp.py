"""
TOTP for platform operators.

The cabinet's second factor is Keystone's (`authentication/mfa.py` shuttles an
auth receipt back and forth). Operators do not exist in Keystone, so the factor
is ours: RFC 6238, 30-second step, one step of drift accepted either way.

The shared secret is encrypted at rest with the same Fernet key as the token
vault — a database dump alone must not be a set of working authenticators.
"""

import secrets

import pyotp
from cryptography.fernet import InvalidToken
from django.core.cache import cache

from apps.authentication.vault import fernet

ISSUER = 'OpenCloud Admin'

# One step (30s) of tolerance. Enough for a phone whose clock has drifted,
# not so much that a shoulder-surfed code stays useful.
VALID_WINDOW = 1

# A code is single-use: without this, anyone who sees it on screen has 30
# seconds to replay it. Keyed by admin id + code, held just past the window.
_REPLAY_PREFIX = 'platform_totp_used:'
_REPLAY_TTL = 30 * (2 * VALID_WINDOW + 1)


def new_secret() -> str:
    return pyotp.random_base32()


def encrypt(secret: str) -> str:
    return fernet().encrypt(secret.encode()).decode()


def decrypt(stored: str) -> str | None:
    if not stored:
        return None
    try:
        return fernet().decrypt(stored.encode()).decode()
    except (InvalidToken, ValueError):
        return None


def provisioning_uri(secret: str, email: str) -> str:
    """The `otpauth://` URI the browser renders as a QR code."""
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=ISSUER)


def verify(secret: str, code: str, *, admin_id: int) -> bool:
    """True once per code. A second presentation of the same code is refused."""
    code = (code or '').strip().replace(' ', '')
    if not secret or not code.isdigit() or len(code) != 6:
        return False

    if not pyotp.TOTP(secret).verify(code, valid_window=VALID_WINDOW):
        return False

    # `cache.add` is the atomic "set if absent" — the first caller wins.
    if not cache.add(f'{_REPLAY_PREFIX}{admin_id}:{code}', 1, timeout=_REPLAY_TTL):
        return False

    return True


def recovery_codes(count: int = 8) -> list[str]:
    """Single-use codes handed out at enrolment, in case the phone is lost."""
    return ['-'.join(secrets.token_hex(2) for _ in range(2)) for _ in range(count)]
