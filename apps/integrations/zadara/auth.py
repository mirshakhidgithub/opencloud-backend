"""
Zadara zCompute authentication & project scoping (Keystone).

- authenticate(): password login. Tries PROJECT scope first (project name ==
  account name on this deployment; overridable) and falls back to DOMAIN scope
  so a user can still sign in even without a matching project.
- exchange_project(): re-scope an existing token to another project WITHOUT a
  password (Keystone token method) — powers the in-app project switcher.
- authenticate_totp(): second MFA step — Keystone's auth-receipt flow, verified
  against a real console capture (HAR): step 1 = password only -> 401 with the
  `openstack-auth-receipt` header + {"required_auth_methods": [["password","totp"]]};
  step 2 = methods:['totp'] ONLY, the receipt echoed back in the same header,
  domain scope, then a token exchange into the project.
- enrollment: when MFA is enforced on the domain and the user has NO device yet,
  a password login answers 401 with a bare body — UNLESS `auto_enable_mfa` is set
  next to identity/scope, in which case Keystone provisions a TOTP secret and
  returns it as {"mfa_secret": "<base32>"} (this is what the native console shows
  as the QR / "MFA Key"). We send that flag only on the password-only step, never
  when a passcode is present, so the secret can never rotate between the QR we
  display and the code the user types back.
- list_projects(): projects the token can enumerate (needs tenant_admin).
"""

from urllib.parse import quote

from .dto import AuthResult, ProjectSummary
from .exceptions import ZadaraError
from .http import request

AUTH_PATH = '/api/v2/identity/auth'
PROJECTS_PATH = '/api/v2/identity/projects'


def _password_identity(account: str, username: str, password: str, totp: str | None = None) -> dict:
    identity = {
        'methods': ['password'],
        'password': {'user': {'domain': {'name': account}, 'name': username, 'password': password}},
    }
    if totp:
        identity['methods'] = ['password', 'totp']
        identity['totp'] = {'user': {'domain': {'name': account}, 'name': username, 'passcode': totp}}
    return identity


def _auth_payload(identity: dict, scope: dict, enroll: bool) -> dict:
    payload = {'auth': {'scope': scope, 'identity': identity}}
    if enroll:
        # Provision a TOTP secret for a user who has none yet (see module docstring).
        payload['auth']['auto_enable_mfa'] = True
    return payload


def _totp_identity(account: str, username: str, passcode: str) -> dict:
    return {
        'methods': ['totp'],
        'totp': {'user': {'domain': {'name': account}, 'name': username, 'passcode': passcode}},
    }


def _is_mfa_required(resp) -> bool:
    """Keystone signals a missing second factor via an auth-receipt (spec: MFA)."""
    if resp.headers.get('openstack-auth-receipt'):
        return True
    try:
        body = resp.json()
    except ValueError:
        return False
    return bool(body.get('receipt') or body.get('required_auth_methods'))


def _mfa_error(resp) -> ZadaraError:
    """Build the mfa_required error, carrying the receipt so step 2 can reuse it."""
    receipt = resp.headers.get('openstack-auth-receipt')
    try:
        body = resp.json()
    except ValueError:
        body = {}
    details = {'receipt': receipt} if receipt else {}
    expires_at = ((body or {}).get('receipt') or {}).get('expires_at')
    if expires_at:
        details['expiresAt'] = expires_at
    return ZadaraError('mfa_required', 'A one-time MFA code is required', 401, details=details)


def _enrollment_secret(resp) -> str | None:
    """The freshly provisioned TOTP secret, when Keystone hands one back."""
    try:
        body = resp.json()
    except ValueError:
        return None
    secret = (body or {}).get('mfa_secret')
    return str(secret) if secret else None


def _enrollment_error(secret: str, account: str, username: str) -> ZadaraError:
    """Carry the secret plus a ready-to-render otpauth URI up to the API layer."""
    label = quote(f'{account}:{username}')
    issuer = quote(account)
    uri = f'otpauth://totp/{label}?secret={secret}&issuer={issuer}&algorithm=SHA1&digits=6&period=30'
    return ZadaraError(
        'mfa_enrollment_required',
        'Multi-factor authentication is required: register your authenticator app',
        401,
        details={'secret': secret, 'otpauthUri': uri},
    )


def _parse_auth_body(body: dict | None, scope: str, fallback_account: str) -> dict:
    token = (body or {}).get('token', {}) or {}
    user = token.get('user', {}) or {}
    project = token.get('project')
    domain = token.get('domain') or (project or {}).get('domain') or user.get('domain') or {}
    roles = [r.get('name') if isinstance(r, dict) else r for r in (token.get('roles') or [])]
    roles = [r for r in roles if r]

    return {
        'user_id': str(user.get('id') or user.get('name') or ''),
        'user_name': str(user.get('name') or ''),
        'email': user.get('email'),
        'account_id': str(domain['id']) if domain.get('id') else None,
        'account_name': str(domain.get('name') or fallback_account),
        'project_id': str(project['id']) if project and project.get('id') else None,
        'project_name': str(project.get('name')) if project and project.get('name') else None,
        'roles': roles,
        'expires_at': token.get('expires_at'),
    }


def _to_result(resp, scope: str, account: str) -> AuthResult:
    subject_token = resp.headers.get('x-subject-token')
    if not subject_token:
        raise ZadaraError('unexpected', 'Zadara did not return an auth token')

    try:
        body = resp.json()
    except ValueError:
        body = None

    parsed = _parse_auth_body(body, scope, account)
    return AuthResult(token=subject_token, scope=scope, **parsed)


def _error_message(resp) -> str:
    try:
        body = resp.json()
    except ValueError:
        return ''
    if not isinstance(body, dict):
        return ''
    error = body.get('error')
    if isinstance(error, dict):
        return str(error.get('message') or '')
    return str(body.get('description') or body.get('message') or '')


# Keystone states the native console keys off, verbatim (taken from its bundle).
_PASSWORD_EXPIRED = 'The password is expired'
_ACCOUNT_LOCKED = 'The account is locked for user'


def _raise_for_auth_status(resp):
    status = resp.status_code
    message = _error_message(resp)

    # A 401 can mean three very different things; only the message tells them apart.
    if message.startswith(_PASSWORD_EXPIRED):
        raise ZadaraError('password_expired', 'Your password has expired and must be changed', 401)
    if message.startswith(_ACCOUNT_LOCKED):
        raise ZadaraError('account_locked', 'This account is locked. Contact your administrator.', 403)

    if status in (401, 403):
        raise ZadaraError('invalid_credentials', 'Invalid account, username or password', status)
    if status == 404:
        raise ZadaraError('account_not_found', 'Account not found', status)
    if status == 429:
        raise ZadaraError('rate_limited', 'Too many login attempts, please try again later', status)
    raise ZadaraError('upstream_error', f'Zadara returned an unexpected status ({status})', status)


def authenticate(
    account: str, username: str, password: str, project: str | None = None, totp: str | None = None
) -> AuthResult:
    if not account or not username or not password:
        raise ZadaraError('invalid_credentials', 'Account, username and password are required')

    project_name = (project or '').strip() or account
    identity = _password_identity(account, username, password, totp)
    enroll = not totp

    # 1) project scope
    project_scope = {'project': {'name': project_name, 'domain': {'name': account}}}
    resp = request('POST', AUTH_PATH, json=_auth_payload(identity, project_scope, enroll))

    if resp.ok:
        return _to_result(resp, 'project', account)

    # MFA needed (password OK, second factor missing) — only when no code sent yet.
    if not totp and _is_mfa_required(resp):
        raise _mfa_error(resp)

    # 2) fallback to domain scope (401 usually = no such project)
    if resp.status_code == 401:
        dresp = request('POST', AUTH_PATH, json=_auth_payload(identity, {'domain': {'name': account}}, enroll))
        if dresp.ok:
            return _to_result(dresp, 'domain', account)
        if not totp and _is_mfa_required(dresp):
            raise _mfa_error(dresp)
        secret = _enrollment_secret(dresp) if not totp else None
        if secret:
            raise _enrollment_error(secret, account, username)
        _raise_for_auth_status(dresp)

    _raise_for_auth_status(resp)


def authenticate_totp(
    account: str, username: str, passcode: str, receipt: str, project: str | None = None
) -> AuthResult:
    """
    Second MFA step. Mirrors what the real console does (captured HAR):
    identity is TOTP-ONLY (no password) and the auth-receipt from step 1 is
    echoed in the `openstack-auth-receipt` header; the request is DOMAIN-scoped
    and the project scope is obtained afterwards via a password-less token
    exchange. Receipts are short-lived (~5 min).
    """
    if not passcode or not receipt:
        raise ZadaraError('invalid_mfa_code', 'A one-time MFA code is required', 401)

    payload = {
        'auth': {
            'identity': _totp_identity(account, username, passcode),
            'scope': {'domain': {'name': account}},
        }
    }
    resp = request('POST', AUTH_PATH, json=payload, headers={'openstack-auth-receipt': receipt})

    if not resp.ok:
        if resp.status_code in (401, 403):
            # Either a wrong/expired code or an expired receipt — both mean
            # "the second factor did not go through"; the SPA restarts if needed.
            raise ZadaraError('invalid_mfa_code', 'Invalid or expired MFA code', 401)
        _raise_for_auth_status(resp)

    domain_result = _to_result(resp, 'domain', account)

    # Resource APIs need a project-scoped token; exchange without re-asking for
    # the password (same as the console does right after the TOTP step).
    try:
        return exchange_project(domain_result.token, (project or '').strip() or account, account)
    except ZadaraError:
        return domain_result  # caller falls back to service-account discovery


def exchange_project(token: str, project_name: str, account: str) -> AuthResult:
    payload = {
        'auth': {
            'identity': {'methods': ['token'], 'token': {'id': token}},
            'scope': {'project': {'name': project_name, 'domain': {'name': account}}},
        }
    }
    resp = request('POST', AUTH_PATH, json=payload)

    if resp.status_code in (401, 403):
        raise ZadaraError('forbidden', 'You cannot access this project', resp.status_code)
    if not resp.ok:
        raise ZadaraError('upstream_error', f'Zadara returned status {resp.status_code}', resp.status_code)

    return _to_result(resp, 'project', account)


def list_projects(token: str) -> list[ProjectSummary]:
    resp = request('GET', PROJECTS_PATH, token=token)

    if resp.status_code == 403:
        raise ZadaraError('forbidden', 'Not authorized to list projects', 403)
    if not resp.ok:
        raise ZadaraError('upstream_error', f'Zadara returned status {resp.status_code}', resp.status_code)

    try:
        data = resp.json()
    except ValueError:
        data = []

    items = data if isinstance(data, list) else data.get('projects', [])
    result = []
    for p in items:
        if not p or p.get('is_domain'):
            continue
        result.append(
            ProjectSummary(
                id=str(p.get('id')),
                name=str(p.get('name') or ''),
                description=p.get('description') or None,
                is_vpc=bool(p.get('is_vpc')),
            )
        )
    return result
