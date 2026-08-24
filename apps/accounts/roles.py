"""Local application roles and mapping from Zadara roles (spec §1.3, §9.1)."""

USER = 'USER'
ADMIN = 'ADMIN'

ROLE_CHOICES = [(USER, 'User'), (ADMIN, 'Admin')]

# Zadara roles that grant local ADMIN — matched EXACTLY, not as substrings.
# Substring matching also caught policy names like `policy:AdministratorAccess`,
# which is a permission bundle, not an administrative role.
_ADMIN_ROLES = frozenset(
    {
        'admin',
        'tenant_admin',
        'account_admin',
        'accountadmin',
        'cloud_admin',
        'superadmin',
        'msp_admin',
    }
)


def resolve_app_role(zadara_roles) -> str:
    """ADMIN here means "administrator of their own account" — never of the
    platform: admin views are scoped to the caller's account server-side."""
    normalized = {str(r).lower() for r in (zadara_roles or [])}
    if normalized & _ADMIN_ROLES:
        return ADMIN

    return USER
