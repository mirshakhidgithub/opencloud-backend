"""
Short-lived MFA tickets (Keystone auth receipts).

Step 1 of an MFA login (password) fails with 401 + an `openstack-auth-receipt`
that step 2 (the TOTP code) must echo back. That receipt is a credential, so it
never reaches the browser: it is kept in the cache — encrypted with the same key
as the token vault — and the SPA only gets an opaque ticket id.
"""

import secrets

from django.core.cache import cache

from .vault import fernet

# Keystone receipts live ~5 minutes; keep the ticket a touch shorter.
TTL_SECONDS = 4 * 60

_KEY_PREFIX = 'mfa_receipt:'


def issue(account: str, username: str, project: str | None, receipt: str) -> str:
    """Store the receipt and return the opaque ticket id for the SPA."""
    ticket = secrets.token_urlsafe(24)
    cache.set(
        f'{_KEY_PREFIX}{ticket}',
        {
            'account': account,
            'username': username,
            'project': project,
            'receipt': fernet().encrypt(receipt.encode()).decode(),
        },
        timeout=TTL_SECONDS,
    )
    return ticket


def load(ticket: str) -> dict | None:
    """Ticket payload with the receipt decrypted, or None when expired/unknown.

    Kept non-destructive on purpose: a mistyped code can be retried with the
    same ticket while the receipt is still valid.
    """
    entry = cache.get(f'{_KEY_PREFIX}{ticket}')
    if not entry:
        return None
    try:
        receipt = fernet().decrypt(entry['receipt'].encode()).decode()
    except Exception:
        return None
    return {**entry, 'receipt': receipt}


def revoke(ticket: str) -> None:
    cache.delete(f'{_KEY_PREFIX}{ticket}')
