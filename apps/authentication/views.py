"""Authentication endpoints (spec §7.2): login, me, logout, refresh, csrf."""

from django.conf import settings
from django.contrib.auth import login, logout
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import ensure_csrf_cookie
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.exceptions import AppError, ForbiddenError
from apps.integrations.zadara import auth as zadara_auth
from apps.integrations.zadara import password as zadara_password
from apps.integrations.zadara import service as zadara_service
from apps.integrations.zadara.exceptions import ZadaraError

from . import mfa, vault
from .services import apply_scope_to_session, me_payload, upsert_user

# Zadara error code → (http status)
_STATUS_BY_CODE = {
    'invalid_credentials': 401,
    'mfa_required': 401,
    'invalid_mfa_code': 401,
    'mfa_enrollment_required': 401,
    'weak_password': 400,
    'password_reused': 400,
    'password_expired': 401,
    'account_locked': 403,
    'invalid_reset_link': 400,
    'mfa_expired': 401,
    'account_not_found': 404,
    'forbidden': 403,
    'rate_limited': 429,
    'timeout': 504,
    'network_error': 502,
    'upstream_error': 502,
}


def _raise_from_zadara(err: ZadaraError):
    raise AppError(message=err.message, code=err.code, status_code=_STATUS_BY_CODE.get(err.code, 502))


@method_decorator(ensure_csrf_cookie, name='get')
class CsrfView(APIView):
    """Sets the csrftoken cookie so the SPA can send X-CSRFToken on writes."""

    permission_classes = [AllowAny]

    def get(self, request):
        return Response({'data': {'detail': 'ok'}})


class LoginView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        data = request.data
        account = (data.get('account') or '').strip()
        username = (data.get('username') or '').strip()
        password = data.get('password') or ''
        project = (data.get('project') or '').strip() or None
        mfa_code = (data.get('mfaCode') or '').strip() or None
        mfa_token = (data.get('mfaToken') or '').strip() or None

        if mfa_token:
            result = self._complete_mfa(mfa_token, mfa_code, account, username)
        else:
            try:
                result = zadara_auth.authenticate(account, username, password, project, totp=mfa_code)
            except ZadaraError as err:
                self._raise_mfa_challenge(err, account, username, project)
                self._raise_mfa_enrollment(err)
                _raise_from_zadara(err)

        # If we couldn't project-scope at login (e.g. a member whose project name
        # differs from the account), use the service account to discover the
        # user's project(s) and re-scope the USER's own token into one of them,
        # so they see their resources. Service is used only for discovery.
        if result.scope != 'project' and not project:
            try:
                projects = zadara_service.get_user_projects(account, username)
                if projects:
                    result = zadara_auth.exchange_project(result.token, projects[0]['name'], account)
            except ZadaraError:
                pass  # keep domain scope; resource views will show a clear message

        user = upsert_user(result)
        if user.is_blocked:
            raise ForbiddenError('This account is blocked.')

        login(request, user, backend='apps.authentication.backends.ZadaraSessionBackend')
        apply_scope_to_session(request, result)
        vault.store(request.session.session_key, result.token)

        return Response({'data': me_payload(request)})

    @staticmethod
    def _raise_mfa_challenge(err: ZadaraError, account: str, username: str, project: str | None):
        """Password accepted but a second factor is due: hand the SPA a ticket."""
        receipt = (err.details or {}).get('receipt')
        if err.code != 'mfa_required' or not receipt:
            return
        raise AppError(
            message=err.message,
            code='mfa_required',
            status_code=401,
            details={'mfaToken': mfa.issue(account, username, project, receipt)},
        )

    @staticmethod
    def _raise_mfa_enrollment(err: ZadaraError):
        """No authenticator registered yet: pass the provisioned secret to the SPA.

        The secret has to reach the browser — it is what the QR encodes — but it
        is useless on its own: the very next request must prove possession with a
        passcode derived from it.
        """
        if err.code != 'mfa_enrollment_required':
            return
        details = err.details or {}
        raise AppError(
            message=err.message,
            code='mfa_enrollment_required',
            status_code=401,
            details={'mfaSecret': details.get('secret'), 'otpauthUri': details.get('otpauthUri')},
        )

    @staticmethod
    def _complete_mfa(mfa_token: str, mfa_code: str | None, account: str, username: str):
        ticket = mfa.load(mfa_token)
        if not ticket:
            raise AppError(
                message='Your sign-in session expired, please start again.',
                code='mfa_expired',
                status_code=401,
            )
        if account.casefold() != ticket['account'].casefold() or username.casefold() != ticket['username'].casefold():
            raise AppError(message='Please start the sign-in again.', code='mfa_expired', status_code=401)

        try:
            result = zadara_auth.authenticate_totp(
                ticket['account'], ticket['username'], mfa_code or '', ticket['receipt'], ticket['project']
            )
        except ZadaraError as err:
            _raise_from_zadara(err)

        mfa.revoke(mfa_token)
        return result


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({'data': me_payload(request)})


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if request.session.session_key:
            vault.delete(request.session.session_key)
        logout(request)
        return Response({'data': {'detail': 'logged out'}})


class RefreshView(APIView):
    """
    Zadara tokens are short-lived (~4h) and cannot be silently refreshed without
    the password. For now this confirms the session is still valid; a real
    refresh will require re-login or a stored refresh mechanism.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        token = vault.get(request.session.session_key) if request.session.session_key else None
        if not token:
            raise AppError(message='Session expired, please sign in again.', code='session_expired', status_code=401)
        return Response({'data': me_payload(request)})


class PasswordResetRequestView(APIView):
    """Ask Zadara to e-mail a reset link (spec §7.2).

    Always answers 200: whether the account/username/e-mail triple matched must
    not be observable, or this endpoint becomes a user-enumeration oracle.
    """

    permission_classes = [AllowAny]

    def post(self, request):
        data = request.data
        account = (data.get('account') or '').strip()
        username = (data.get('username') or '').strip()
        email = (data.get('email') or '').strip()

        if account and username and email:
            try:
                zadara_password.request_reset(account, username, email, settings.PASSWORD_RESET_URL_TEMPLATE)
            except ZadaraError:
                pass  # logged in the integration; never surfaced to the caller

        return Response({'data': {'detail': 'If the details match an account, a reset link has been sent.'}})


class PasswordResetVerifyView(APIView):
    """Complete a reset with the secret from the e-mailed link."""

    permission_classes = [AllowAny]

    def post(self, request):
        secret = (request.data.get('secret') or '').strip()
        new_password = request.data.get('newPassword') or ''

        if not secret or not new_password:
            raise AppError(message='A reset link and a new password are required.', code='invalid_request', status_code=400)

        try:
            zadara_password.verify_reset(secret, new_password)
        except ZadaraError as err:
            _raise_from_zadara(err)

        return Response({'data': {'detail': 'Password updated. You can sign in with it now.'}})


class PasswordChangeView(APIView):
    """Change one's own password, then force a fresh sign-in.

    Open to anonymous callers on purpose: a user whose password has expired
    cannot sign in, and this is their only way out. Nothing is granted here that
    the current password does not already grant — Zadara's own endpoint takes no
    token either. Signed-in callers do not have to repeat who they are.
    """

    permission_classes = [AllowAny]

    def post(self, request):
        data = request.data
        current_password = data.get('currentPassword') or ''
        new_password = data.get('newPassword') or ''

        # Signed in: the cloud expects this call to carry the session's token.
        session_token = vault.get(request.session.session_key) if request.session.session_key else None

        if request.user.is_authenticated:
            account, username = request.user.account, request.user.username
        else:
            account = (data.get('account') or '').strip()
            username = (data.get('username') or '').strip()

        if not account or not username or not current_password or not new_password:
            raise AppError(
                message='Account, username, the current password and a new password are required.',
                code='invalid_request',
                status_code=400,
            )

        try:
            zadara_password.change_password(
                account,
                username,
                current_password,
                new_password,
                totp=(data.get('mfaCode') or '').strip() or None,
                token=session_token,
            )
        except ZadaraError as err:
            _raise_from_zadara(err)

        # Existing tokens were issued against the old password: drop the session
        # rather than leave a half-valid one behind.
        if request.session.session_key:
            vault.delete(request.session.session_key)
        logout(request)

        return Response({'data': {'detail': 'Password changed. Please sign in again.', 'reauthenticate': True}})
