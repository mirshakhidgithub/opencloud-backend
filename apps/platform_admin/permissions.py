"""
Who may do what inside the admin panel.

Note what these do NOT do: they never look at `accounts.User.app_role`. That
field means "administrator of their own account" and is derived from whatever
roles Zadara reports — the cloud must not be able to promote anyone into this
panel.
"""

from rest_framework.permissions import BasePermission

from .models import PlatformAdmin


class IsPlatformAdmin(BasePermission):
    """Any signed-in operator. Read access to the whole platform."""

    message = 'Platform operator sign-in required.'

    def has_permission(self, request, view):
        return isinstance(request.user, PlatformAdmin) and request.user.is_active


class CanWritePlatform(IsPlatformAdmin):
    """Operators whose role may change platform state (OWNER, OPS)."""

    message = 'This action needs the Owner or Operations role.'

    def has_permission(self, request, view):
        return super().has_permission(request, view) and request.user.can_write


class IsPlatformOwner(IsPlatformAdmin):
    """Managing other operators is the Owner's alone."""

    message = 'This action needs the Owner role.'

    def has_permission(self, request, view):
        return super().has_permission(request, view) and request.user.role == 'OWNER'
