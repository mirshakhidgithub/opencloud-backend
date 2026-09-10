"""
Sign-in for platform operators: password, then a second factor. Always both.

The second factor is TOTP, except for an operator an owner has given a static
access code (`setplatformadmincode`) — for them the code replaces TOTP. Either
way step 1 only ever yields a ticket, and only step 2 yields a session.

Enrolment is folded into the first sign-in rather than offered as a setting an
operator might never open. Until the second factor is confirmed there is no
session — an operator with a password alone can reach nothing.
"""

from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import ensure_csrf_cookie
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.exceptions import AppError

from . import services, tickets, totp
from .authentication import PlatformAPIView, end_session, start_session
from .models import AdminAction, PlatformAdmin
from .permissions import IsPlatformAdmin

# Everything step 1 can go wrong with answers the same way. Which of "no such
# operator", "wrong password" and "deactivated" it was is in the action log, not
# in the response: the sign-in page is the one place we do not help people guess.
_BAD_CREDENTIALS = 'Invalid e-mail or password.'


@method_decorator(ensure_csrf_cookie, name='get')
class CsrfView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        return Response({'data': {'detail': 'ok'}})


class LoginView(APIView):
    """Step 1 — password. Never returns a session, only a ticket for step 2."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        email = (request.data.get('email') or '').strip().lower()
        password = request.data.get('password') or ''

        admin = PlatformAdmin.objects.filter(email=email).first()

        if admin is None or not admin.is_active:
            services.record(
                request, 'admin.login', outcome=AdminAction.FAILURE, error_code='unknown_operator',
                detail={'email': email},
            )
            raise AppError(message=_BAD_CREDENTIALS, code='invalid_credentials', status_code=401)

        if admin.is_locked:
            services.record(request, 'admin.login', actor=admin, outcome=AdminAction.FAILURE, error_code='locked')
            raise AppError(
                message='Too many failed attempts. Try again in a few minutes.',
                code='account_locked',
                status_code=403,
            )

        if not admin.check_password(password):
            admin.note_failure()
            services.record(
                request, 'admin.login', actor=admin, outcome=AdminAction.FAILURE, error_code='bad_password'
            )
            raise AppError(message=_BAD_CREDENTIALS, code='invalid_credentials', status_code=401)

        if admin.uses_access_code:
            return Response({'data': {'stage': 'code', 'ticket': tickets.issue(admin.pk, enrolling=False)}})

        if admin.totp_confirmed_at and admin.totp_secret:
            return Response({'data': {'stage': 'totp', 'ticket': tickets.issue(admin.pk, enrolling=False)}})

        # First sign-in: hand over a fresh secret. It has to reach the browser —
        # it is what the QR encodes — but it buys nothing on its own, because the
        # very next request must prove possession of it, and it is not stored
        # against the operator until that proof arrives.
        secret = totp.new_secret()
        ticket = tickets.issue(admin.pk, enrolling=True, pending_secret_encrypted=totp.encrypt(secret))

        return Response(
            {
                'data': {
                    'stage': 'enroll',
                    'ticket': ticket,
                    'secret': secret,
                    'otpauthUri': totp.provisioning_uri(secret, admin.email),
                }
            }
        )


class LoginVerifyView(APIView):
    """Step 2 — the six digits, or the static access code. This is the only
    place a session is created."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        ticket = (request.data.get('ticket') or '').strip()
        code = (request.data.get('code') or '').strip()

        payload = tickets.load(ticket)
        if not payload:
            raise AppError(
                message='This sign-in attempt expired. Start again.', code='mfa_expired', status_code=401
            )

        admin = PlatformAdmin.objects.filter(pk=payload['admin_id'], is_active=True).first()
        if admin is None:
            tickets.revoke(ticket)
            raise AppError(message=_BAD_CREDENTIALS, code='invalid_credentials', status_code=401)

        # The lockout has to hold here too, not only at the password. A ticket
        # lives four minutes, and without this check it is four minutes of
        # unlimited guesses at the second factor — harmless against a TOTP code
        # that changes every 30 seconds, fatal against a static one.
        if admin.is_locked:
            tickets.revoke(ticket)
            services.record(request, 'admin.login', actor=admin, outcome=AdminAction.FAILURE, error_code='locked')
            raise AppError(
                message='Too many failed attempts. Try again in a few minutes.',
                code='account_locked',
                status_code=403,
            )

        # Decided by the operator's settings now, not by the stage step 1
        # announced: whatever factor this operator is on is the one that counts.
        if admin.uses_access_code:
            if not admin.check_access_code(code):
                admin.note_failure()
                services.record(
                    request, 'admin.login', actor=admin, outcome=AdminAction.FAILURE, error_code='bad_access_code'
                )
                raise AppError(message='That code is not valid.', code='invalid_access_code', status_code=401)
        else:
            enrolling = payload['enrolling']
            secret = totp.decrypt(payload['pending_secret']) if enrolling else totp.decrypt(admin.totp_secret)

            if not secret or not totp.verify(secret, code, admin_id=admin.pk):
                admin.note_failure()
                services.record(
                    request, 'admin.login', actor=admin, outcome=AdminAction.FAILURE, error_code='bad_totp'
                )
                raise AppError(message='That code is not valid.', code='invalid_mfa_code', status_code=401)

            if enrolling:
                admin.totp_secret = totp.encrypt(secret)
                admin.totp_confirmed_at = timezone.now()
                admin.save(update_fields=['totp_secret', 'totp_confirmed_at'])
                services.record(request, 'admin.totp.enroll', actor=admin)

        tickets.revoke(ticket)
        start_session(request, admin)
        admin.note_success(services.client_ip(request))
        services.record(request, 'admin.login', actor=admin)

        return Response({'data': services.me_payload(admin)})


class MeView(PlatformAPIView):
    permission_classes = [IsPlatformAdmin]

    def get(self, request):
        return Response({'data': services.me_payload(request.user)})


class LogoutView(PlatformAPIView):
    permission_classes = [IsPlatformAdmin]

    def post(self, request):
        services.record(request, 'admin.logout')
        end_session(request)
        return Response({'data': {'detail': 'ok'}})


class PasswordChangeView(PlatformAPIView):
    """Change your own password. The session survives; the other ones do not
    (they cannot — sessions are per-browser here and there is nothing shared)."""

    permission_classes = [IsPlatformAdmin]

    def post(self, request):
        admin = request.user
        current = request.data.get('currentPassword') or ''
        new = request.data.get('newPassword') or ''

        if not admin.check_password(current):
            services.record(request, 'admin.password.change', outcome=AdminAction.FAILURE, error_code='bad_password')
            raise AppError(message='Current password is not correct.', code='invalid_credentials', status_code=401)

        problem = password_problem(new)
        if problem:
            raise AppError(message=problem, code='weak_password', status_code=400)

        admin.set_password(new)
        admin.save(update_fields=['password_hash'])
        services.record(request, 'admin.password.change')

        return Response({'data': {'detail': 'ok'}})


def password_problem(password: str) -> str | None:
    """The operator password rule, in one place so the CLI and the API agree."""
    if len(password or '') < 12:
        return 'Password must be at least 12 characters.'
    if password.isdigit() or password.isalpha():
        return 'Password must mix letters, digits and punctuation.'
    return None


# Shorter than a password on purpose — it is typed at every sign-in — and safe
# only because the lockout covers step 2: five wrong guesses, then 15 minutes.
ACCESS_CODE_MIN_LENGTH = 6


def access_code_problem(admin: PlatformAdmin, code: str) -> str | None:
    """The static access code rule. `admin` must already carry its password."""
    if len(code or '') < ACCESS_CODE_MIN_LENGTH:
        return f'The access code must be at least {ACCESS_CODE_MIN_LENGTH} characters.'
    if admin.check_password(code):
        # Two factors that are the same string are one factor.
        return 'The access code must differ from the password.'
    return None
