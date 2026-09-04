"""
Session authentication for the admin panel, on its own session key.

Django's own login machinery writes `_auth_user_id` and resolves it against
AUTH_USER_MODEL. Operators are not that model, so this reads a key of its own —
which is the whole isolation story: a cabinet session cookie replayed at
admin.opencloud.uz carries `_auth_user_id` and no `platform_admin_id`, so it
authenticates as nobody here. The reverse holds too.

CSRF is enforced exactly as DRF's SessionAuthentication does; skipping it
because "this is a different session" would leave every write open to a
cross-site POST.
"""

from rest_framework.authentication import BaseAuthentication, SessionAuthentication
from rest_framework.generics import ListAPIView
from rest_framework.views import APIView

from .models import PlatformAdmin

SESSION_KEY = 'platform_admin_id'

# A working day, then sign in again. Deliberately not `set_expiry(0)`: that ties
# the session to the browser window, which for an operator who never closes
# theirs is no bound at all.
SESSION_SECONDS = 8 * 60 * 60


def start_session(request, admin: PlatformAdmin) -> None:
    """Log an operator in. Cycles the key so a pre-login cookie cannot be fixed."""
    request.session.cycle_key()
    request.session[SESSION_KEY] = admin.pk
    request.session.set_expiry(SESSION_SECONDS)


def end_session(request) -> None:
    request.session.flush()


class PlatformSessionAuthentication(BaseAuthentication):
    def authenticate(self, request):
        admin_id = request.session.get(SESSION_KEY)
        if not admin_id:
            return None

        admin = PlatformAdmin.objects.filter(pk=admin_id, is_active=True).first()
        if admin is None:
            # Deactivated or deleted mid-session: drop the cookie rather than
            # leave it pointing at a row that will not come back.
            request.session.flush()
            return None

        self._enforce_csrf(request)
        return (admin, None)

    @staticmethod
    def _enforce_csrf(request):
        """Reuse DRF's own check so the two paths cannot drift apart."""
        SessionAuthentication().enforce_csrf(request)


class PlatformAPIView(APIView):
    """
    Base for every view in this app.

    `authentication_classes` is set here rather than globally on purpose. Adding
    `PlatformSessionAuthentication` to DEFAULT_AUTHENTICATION_CLASSES would make
    an operator session authenticate against the cabinet's own endpoints too —
    where `request.user` is assumed to be `accounts.User` and carry an account,
    a project scope and a Zadara token in the vault. An operator has none of
    those. Keeping the two authenticators apart is what stops that from ever
    being discovered at runtime.
    """

    authentication_classes = [PlatformSessionAuthentication]


class PlatformListAPIView(ListAPIView):
    """`PlatformAPIView` for the paginated list endpoints."""

    authentication_classes = [PlatformSessionAuthentication]
