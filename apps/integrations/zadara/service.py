"""
Service-mode access to Zadara (spec §5.4, §4.3).

Authenticates the MSP read-only service account, scoped to ZADARA_SERVICE_PROJECT,
which yields an `msp_admin` (cluster-wide READ) token. Used ONLY server-side for:
  - discovering a user's projects/roles at login (members can't list their own),
  - platform-wide admin overview (all VMs / accounts).

The service token is cached (short TTL) and refreshed automatically. It never
leaves the server. Every caller must still enforce the requesting user's role.
"""

import hashlib
import logging
import time

from django.conf import settings
from django.core.cache import cache

from . import auth as zauth
from .exceptions import ZadaraError
from .http import request

logger = logging.getLogger('zadara')

# Process-local cache. For multi-worker prod, back this with Redis (encrypted).
_TTL_SECONDS = 3 * 60 * 60
_cache = {'token': None, 'exp': 0.0}


def get_service_token(force: bool = False) -> str:
    now = time.time()
    if not force and _cache['token'] and now < _cache['exp'] - 120:
        return _cache['token']

    if not (settings.ZADARA_SERVICE_USERNAME and settings.ZADARA_SERVICE_PASSWORD):
        raise ZadaraError('unexpected', 'Service account is not configured')

    result = zauth.authenticate(
        settings.ZADARA_SERVICE_ACCOUNT,
        settings.ZADARA_SERVICE_USERNAME,
        settings.ZADARA_SERVICE_PASSWORD,
        settings.ZADARA_SERVICE_PROJECT or None,
    )
    _cache['token'] = result.token
    _cache['exp'] = now + _TTL_SECONDS
    logger.info('zadara service token refreshed (scope=%s, roles=%s)', result.scope, result.roles)
    return result.token


def _svc_get(path: str):
    """GET with the service token, retrying once on a possibly-expired token."""
    resp = request('GET', path, token=get_service_token())
    if resp.status_code == 401:
        resp = request('GET', path, token=get_service_token(force=True))
    if not resp.ok:
        raise ZadaraError('upstream_error', f'Zadara returned {resp.status_code} for {path}', resp.status_code)
    try:
        return resp.json()
    except ValueError:
        return None


# The account and project directory changes when an operator adds one, which is
# rare and never something the cabinet does — it has no create-account or
# create-project path. So it is cached for minutes rather than seconds.
#
# User lists are deliberately NOT cached: an administrator who has just created
# someone must see them, and those writes go out with the admin's own token, so
# this scope would not know to expire.
DIRECTORY_CACHE_TTL = 300

_DIRECTORY_PREFIX = 'zadara:svc'


def _svc_get_cached(path: str, ttl: int = DIRECTORY_CACHE_TTL):
    """`_svc_get` for near-static directory reads. One scope: the service token."""
    key = f'{_DIRECTORY_PREFIX}:{hashlib.sha256(path.encode()).hexdigest()[:16]}'

    hit = cache.get(key)
    if hit is not None:
        return hit

    data = _svc_get(path)
    if data is not None:
        cache.set(key, data, ttl)

    return data


def list_domains() -> list[dict]:
    """Every account on the cluster, as {id, name}. Service mode only."""
    data = _svc_get_cached('/api/v2/identity/domains')
    items = data if isinstance(data, list) else (data or {}).get('domains', [])

    return [{'id': str(d['id']), 'name': d.get('name', '')} for d in items if d.get('id')]


def resolve_domain_id(account_name: str) -> str | None:
    # This deployment rejects ?name= filters on /domains; list and match locally.
    data = _svc_get_cached('/api/v2/identity/domains')
    items = data if isinstance(data, list) else (data or {}).get('domains', [])
    for d in items:
        if str(d.get('name', '')).lower() == account_name.lower():
            return str(d['id'])
    return None


def resolve_user(account_name: str, username: str) -> dict | None:
    """Find a user by account (domain) + name. Returns id/email/default_project_id."""
    domain_id = resolve_domain_id(account_name)
    if not domain_id:
        return None
    data = _svc_get(f'/api/v2/identity/users?domain_id={domain_id}')
    items = data if isinstance(data, list) else (data or {}).get('users', [])
    u = next((x for x in items if str(x.get('name', '')).lower() == username.lower()), None)
    if not u:
        return None
    return {
        'id': str(u.get('id')),
        'name': u.get('name'),
        'email': u.get('email'),
        'domain_id': domain_id,
        'default_project_id': u.get('default_project_id'),
    }


def get_user_project_ids(user_id: str) -> list[str]:
    """Distinct project ids the user has any role in (spec: member project discovery)."""
    data = _svc_get(f'/api/v2/identity/role_assignments?user.id={user_id}')
    items = data if isinstance(data, list) else (data or {}).get('role_assignments', [])
    ids = []
    for a in items:
        pid = (((a or {}).get('scope') or {}).get('project') or {}).get('id')
        if pid and pid not in ids:
            ids.append(str(pid))
    return ids


def list_domain_projects(domain_id: str) -> dict[str, str]:
    """Map project_id -> name for a domain (to label discovered project ids)."""
    data = _svc_get_cached(f'/api/v2/identity/projects?domain_id={domain_id}')
    items = data if isinstance(data, list) else (data or {}).get('projects', [])
    return {str(p['id']): p.get('name', '') for p in items if p.get('id')}


def get_user_projects(account_name: str, username: str) -> list[dict]:
    """
    High-level discovery: given account + username, return the projects the user
    can access as [{id, name}] — works even for plain members.
    """
    user = resolve_user(account_name, username)
    if not user:
        return []
    project_ids = get_user_project_ids(user['id'])
    names = list_domain_projects(user['domain_id'])
    return [{'id': pid, 'name': names.get(pid, pid)} for pid in project_ids]


def list_domain_users(domain_id: str) -> list[dict]:
    """Users of one account, as the admin views need them."""
    data = _svc_get(f'/api/v2/identity/users?domain_id={domain_id}')
    items = data if isinstance(data, list) else (data or {}).get('users', [])

    return [
        {
            'id': str(u.get('id') or ''),
            'name': u.get('name') or '',
            'email': u.get('email'),
            'enabled': bool(u.get('enabled')),
            'mfaEnabled': bool(u.get('mfa_enabled')),
            'isLocal': bool(u.get('is_local')),
            'systemUser': bool(u.get('system_user')),
            'createdAt': u.get('created_at'),
            'passwordExpiresAt': u.get('password_expires_at'),
            'defaultProjectId': u.get('default_project_id'),
        }
        for u in items
        if isinstance(u, dict)
    ]


def list_domain_project_details(domain_id: str) -> list[dict]:
    """Projects of one account, with the fields a tenant list wants."""
    data = _svc_get_cached(f'/api/v2/identity/projects?domain_id={domain_id}')
    items = data if isinstance(data, list) else (data or {}).get('projects', [])

    return [
        {
            'id': str(p.get('id') or ''),
            'name': p.get('name') or '',
            'description': p.get('description') or None,
            'enabled': bool(p.get('enabled', True)),
            'isVpc': bool(p.get('is_vpc')),
        }
        for p in items
        if isinstance(p, dict) and not p.get('is_domain')
    ]
