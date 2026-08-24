"""Auth service helpers: upsert local user, session scope, `me` payload."""

from django.contrib.auth import get_user_model

from apps.accounts.roles import resolve_app_role
from apps.integrations.zadara.dto import AuthResult

User = get_user_model()

SESSION_PROJECT_ID = 'zadara_project_id'
SESSION_PROJECT_NAME = 'zadara_project_name'
SESSION_SCOPE = 'zadara_scope'
SESSION_ROLES = 'zadara_roles'


def upsert_user(result: AuthResult) -> User:
    """Create or update the local user linked to the Zadara identity (spec §11.4)."""
    role = resolve_app_role(result.roles)
    user, _ = User.objects.update_or_create(
        zadara_user_id=result.user_id,
        defaults={
            'username': result.user_name,
            'email': result.email or None,
            'account': result.account_name,
            'account_id': result.account_id or '',
            'app_role': role,
        },
    )
    return user


def apply_scope_to_session(request, result: AuthResult) -> None:
    """Store the current project/scope/roles on the session; sync the user's role."""
    request.session[SESSION_PROJECT_ID] = result.project_id
    request.session[SESSION_PROJECT_NAME] = result.project_name
    request.session[SESSION_SCOPE] = result.scope
    request.session[SESSION_ROLES] = result.roles

    new_role = resolve_app_role(result.roles)
    if request.user.app_role != new_role:
        request.user.app_role = new_role
        request.user.save(update_fields=['app_role'])


def me_payload(request) -> dict:
    """User profile for the frontend — same shape the Next.js session expects."""
    u = request.user
    s = request.session
    return {
        'name': u.username,
        'email': u.email,
        'appRole': u.app_role,
        'zadaraUserId': u.zadara_user_id,
        'zadaraAccount': u.account,
        'zadaraAccountId': u.account_id or None,
        'zadaraProjectId': s.get(SESSION_PROJECT_ID),
        'zadaraProjectName': s.get(SESSION_PROJECT_NAME),
        'zadaraRoles': s.get(SESSION_ROLES, []),
    }
