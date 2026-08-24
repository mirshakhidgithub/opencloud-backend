"""
RBAC permissions (spec §9.1). Authorization is always based on the REQUESTING
user's role — even when the backend later uses a powerful service token, a
non-admin can never reach admin data.
"""

from rest_framework.permissions import BasePermission


def _app_role(user):
    return getattr(user, 'app_role', None)


class IsAdmin(BasePermission):
    """Allow only authenticated users with the ADMIN application role."""

    message = 'Administrator role required.'

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and _app_role(request.user) == 'ADMIN')
