"""
Auth backend for Zadara-backed session login.

We authenticate against Zadara in the view (not here) and then call
`django.contrib.auth.login(request, user, backend=...)`. This backend only needs
to resolve a user from the session (`get_user`).
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.backends import BaseBackend

User = get_user_model()


class ZadaraSessionBackend(BaseBackend):
    def authenticate(self, request, **kwargs):  # noqa: D401 - login is done in the view
        return None

    def get_user(self, user_id):
        try:
            return User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return None
