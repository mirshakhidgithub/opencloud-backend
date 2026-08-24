"""
Password lifecycle against Zadara's Keystone (spec §7.2).

Routes and payloads were recovered from the console's own bundle and confirmed
against the live API, which names its required fields on a 400:

  POST /api/v2/identity/users/myself/password      change own password: token in
                                                   the header, body is just
                                                   {original_password, password}
  POST /api/v2/identity/auth/password-reset        e-mail a reset link; requires
                                                   domain_name, user_name,
                                                   user_email, url_template
  POST /api/v2/identity/auth/password-reset/verify {secret, new_password}

`url_template` is the link the e-mail will carry, with a literal `$secret`
placeholder the server substitutes — so the landing page is ours to choose.
"""

import logging

from .exceptions import ZadaraError
from .http import request

# What the console actually calls (captured): a plain, token-authenticated POST.
CHANGE_PATH = '/api/v2/identity/users/myself/password'

# The auth-envelope variant. Only reachable while signed out — an expired
# password at sign-in, where there is no token to authenticate with. Unverified.
CHANGE_VIA_AUTH_PATH = '/api/v2/identity/auth/password'
RESET_PATH = '/api/v2/identity/auth/password-reset'
VERIFY_PATH = f'{RESET_PATH}/verify'

SECRET_PLACEHOLDER = '$secret'

logger = logging.getLogger('zadara')


def payload_for(identity: dict, account: str) -> dict:
    return {'auth': {'identity': identity, 'scope': {'domain': {'name': account}}}}


def _description(resp) -> str:
    try:
        body = resp.json()
    except ValueError:
        return ''
    if isinstance(body, dict):
        return str(body.get('description') or (body.get('error') or {}).get('message') or '')
    return ''


# Verbatim prefix the native console keys off for a re-used password; the cluster
# remembers the last `passwords_remembered` (4 here) and refuses them.
_PASSWORD_REUSED = 'Password validation error: The new password cannot be'


def _looks_like_policy_violation(text: str) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in ('password', 'character', 'length', 'digit', 'special'))


def _raise_for_new_password(resp) -> None:
    """Map a rejected new password to a code the UI can phrase precisely."""
    description = _description(resp)

    if description.startswith(_PASSWORD_REUSED):
        raise ZadaraError('password_reused', 'You cannot reuse one of your last passwords', 400)
    if resp.status_code == 400 and _looks_like_policy_violation(description):
        raise ZadaraError('weak_password', description or 'The new password was rejected', 400)


def change_password(
    account: str,
    username: str,
    current_password: str,
    new_password: str,
    totp: str | None = None,
    token: str | None = None,
) -> None:
    """Change one's own password.

    Signed in — the normal case — this is a token-authenticated POST with just
    `{original_password, password}`, exactly as the native console does it
    (captured: 204, no MFA code involved even for an MFA-enabled user). The
    cloud revokes existing tokens afterwards, so the caller must sign in again.

    Signed out, the only caller is the expired-password path at sign-in: there
    is no token there, so it falls back to the auth-envelope form. That branch
    has never been exercised against a real expired password.
    """
    if token:
        resp = request(
            'POST',
            CHANGE_PATH,
            token=token,
            json={'original_password': current_password, 'password': new_password},
        )
    else:
        user = {'domain': {'name': account}, 'name': username, 'password': current_password}
        identity = {'methods': ['password'], 'new_password': new_password, 'password': {'user': user}}

        if totp:
            identity['methods'] = ['password', 'totp']
            identity['totp'] = {'user': {'domain': {'name': account}, 'name': username, 'passcode': totp}}

        resp = request('POST', CHANGE_VIA_AUTH_PATH, json=payload_for(identity, account))

    if resp.ok:
        return

    _raise_for_new_password(resp)

    if resp.status_code in (401, 403):
        raise ZadaraError('invalid_credentials', 'Your current password is not correct', resp.status_code)
    if resp.status_code == 400:
        raise ZadaraError('weak_password', _description(resp) or 'The new password was rejected', 400)
    raise ZadaraError('upstream_error', f'Zadara returned status {resp.status_code}', resp.status_code)


def request_reset(account: str, username: str, email: str, url_template: str) -> None:
    """Ask Zadara to e-mail a reset link.

    Raises only on our own misuse; upstream refusals are logged and swallowed by
    the caller so the response cannot be used to probe which users exist.
    """
    if SECRET_PLACEHOLDER not in url_template:
        raise ZadaraError('unexpected', f'url_template must contain {SECRET_PLACEHOLDER}')

    payload = {
        'domain_name': account,
        'user_name': username,
        'user_email': email,
        'url_template': url_template,
    }
    resp = request('POST', RESET_PATH, json=payload)

    if resp.ok:
        return

    # Wrong account/username/e-mail combinations land here; never tell the caller.
    logger.info('password reset refused: %s %s', resp.status_code, _description(resp)[:120])
    raise ZadaraError('reset_refused', 'Zadara refused the reset request', resp.status_code)


def verify_reset(secret: str, new_password: str) -> None:
    """Complete a reset with the secret from the e-mailed link."""
    resp = request('POST', VERIFY_PATH, json={'secret': secret, 'new_password': new_password})

    if resp.ok:
        return

    _raise_for_new_password(resp)

    if resp.status_code in (400, 401, 403, 404):
        raise ZadaraError('invalid_reset_link', 'This reset link is invalid or has expired', resp.status_code)
    raise ZadaraError('upstream_error', f'Zadara returned status {resp.status_code}', resp.status_code)
