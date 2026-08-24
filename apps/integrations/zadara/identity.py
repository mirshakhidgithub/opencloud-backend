"""
User and project administration in Zadara (spec §4.2).

Shapes captured from the native console, so they are facts rather than guesses:

  POST   /api/v2/identity/users
         {name, email, password, project_id, domain_id, must_change_password}
  PUT    /api/v2/identity/users/{id}
         {name?, email?, enabled?, password?, password_never_expires?}
  DELETE /api/v2/identity/users/{id}                       -> 200 with a message
  PUT    /api/v2/identity/projects/{pid}/users/{uid}/roles/{role}   (no body) -> 204

Everything here is called with the **administrator's own token**, never the
service token: the cloud must judge the request by the rights of the person who
asked for it. That also means a tenant admin physically cannot touch another
account, whatever our own filtering does or fails to do.
"""

from .exceptions import ZadaraError
from .http import request

USERS_PATH = '/api/v2/identity/users'
PROJECTS_PATH = '/api/v2/identity/projects'

DEFAULT_ROLE = '_member_'


def _result(resp, action: str) -> dict | None:
    if resp.status_code in (401, 403):
        raise ZadaraError('forbidden', f'You are not allowed to {action}', resp.status_code)
    if resp.status_code == 404:
        raise ZadaraError('not_found', 'That user or project no longer exists', 404)
    if resp.status_code == 409:
        raise ZadaraError('conflict', 'A user with that name already exists', 409)
    if resp.status_code == 400:
        try:
            description = (resp.json() or {}).get('description') or ''
        except ValueError:
            description = ''
        raise ZadaraError('invalid_request', description or 'The cloud rejected these details', 400)
    if not resp.ok:
        raise ZadaraError('upstream_error', f'Zadara returned status {resp.status_code}', resp.status_code)

    try:
        body = resp.json()
    except ValueError:
        return None

    return body if isinstance(body, dict) else None


def create_user(
    token: str,
    domain_id: str,
    name: str,
    email: str,
    password: str,
    must_change_password: bool = True,
    project_id: str = '',
) -> dict | None:
    payload = {
        'name': name,
        'email': email,
        'password': password,
        'project_id': project_id,
        'domain_id': domain_id,
        'must_change_password': must_change_password,
    }

    return _result(request('POST', USERS_PATH, token=token, json=payload), 'create users')


def update_user(token: str, user_id: str, **fields) -> dict | None:
    """Change what was passed and nothing else — omitted fields stay as they are."""
    payload = {k: v for k, v in fields.items() if v is not None}
    if not payload:
        raise ZadaraError('invalid_request', 'Nothing to change', 400)

    return _result(request('PUT', f'{USERS_PATH}/{user_id}', token=token, json=payload), 'change this user')


def delete_user(token: str, user_id: str) -> None:
    _result(request('DELETE', f'{USERS_PATH}/{user_id}', token=token), 'delete this user')


def grant_project_role(token: str, project_id: str, user_id: str, role: str = DEFAULT_ROLE) -> None:
    path = f'{PROJECTS_PATH}/{project_id}/users/{user_id}/roles/{role}'
    _result(request('PUT', path, token=token), 'grant roles in this project')


def revoke_project_role(token: str, project_id: str, user_id: str, role: str = DEFAULT_ROLE) -> None:
    path = f'{PROJECTS_PATH}/{project_id}/users/{user_id}/roles/{role}'
    _result(request('DELETE', path, token=token), 'revoke roles in this project')
