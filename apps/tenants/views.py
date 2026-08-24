"""Project (tenant) endpoints: list + in-app switch (spec §3.x, §8.2)."""

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.authentication import vault
from apps.authentication.services import apply_scope_to_session, me_payload
from apps.common.exceptions import AppError
from apps.integrations.zadara import auth as zadara_auth
from apps.integrations.zadara import service as zadara_service
from apps.integrations.zadara.exceptions import ZadaraError


def _require_token(request) -> str:
    token = vault.get(request.session.session_key) if request.session.session_key else None
    if not token:
        raise AppError(message='Session expired, please sign in again.', code='session_expired', status_code=401)
    return token


class ProjectsView(APIView):
    """
    GET /api/v1/user/projects — projects the user can switch into.
    Needs a tenant_admin-scoped token; plain members get an empty list.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        # Prefer service-mode discovery so it works for plain members too.
        try:
            discovered = zadara_service.get_user_projects(request.user.account, request.user.username)
            data = [{'id': p['id'], 'name': p['name']} for p in discovered]
            return Response({'data': data, 'meta': {'total': len(data)}})
        except ZadaraError:
            pass

        # Fallback: list via the user's own token (works only for tenant admins).
        token = _require_token(request)
        try:
            projects = zadara_auth.list_projects(token)
        except ZadaraError as err:
            if err.code == 'forbidden':
                return Response({'data': [], 'meta': {'total': 0}})
            raise AppError(message='Failed to load projects', code=err.code, status_code=502)

        data = [
            {'id': p.id, 'name': p.name, 'description': p.description, 'isVpc': p.is_vpc}
            for p in projects
        ]
        return Response({'data': data, 'meta': {'total': len(data)}})


class ProjectSwitchView(APIView):
    """POST /api/v1/user/projects/switch {project} — re-scope via token exchange."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        project_name = (request.data.get('project') or '').strip()
        if not project_name:
            raise AppError(message='project is required', code='validation_error', status_code=400)

        token = _require_token(request)
        try:
            result = zadara_auth.exchange_project(token, project_name, request.user.account)
        except ZadaraError as err:
            status = 403 if err.code == 'forbidden' else 502
            raise AppError(message=err.message, code=err.code, status_code=status)

        vault.store(request.session.session_key, result.token)
        apply_scope_to_session(request, result)

        return Response({'data': me_payload(request)})
