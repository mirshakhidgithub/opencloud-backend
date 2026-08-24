"""Dashboard summary (spec §3.2): the numbers behind the console home page."""

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.authentication import vault
from apps.common.exceptions import AppError
from apps.integrations.zadara import resources as zadara_resources
from apps.integrations.zadara.exceptions import ZadaraError

RUNNING = ('active', 'running')


class DashboardView(APIView):
    """
    GET /api/v1/user/dashboard — counts for the current project.

    Read with the user's own project-scoped token, so the answer is naturally
    limited to what they may see. Each section is fetched independently and a
    refusal degrades that section only: a user without volume rights should
    still get their machine count rather than an empty page.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        token = vault.get(request.session.session_key) if request.session.session_key else None
        if not token:
            raise AppError(message='Session expired, please sign in again.', code='session_expired', status_code=401)

        summary = {}
        unavailable = []

        def section(name, fetch):
            try:
                return fetch()
            except ZadaraError:
                unavailable.append(name)

                return None

        vms = section('vms', lambda: zadara_resources.list_vms(token))
        summary['vms'] = (
            {
                'total': len(vms),
                'running': sum(1 for v in vms if v['status'].lower() in RUNNING),
                'vcpus': sum(v['vcpus'] for v in vms),
                'ramMB': sum(v['ramMB'] for v in vms),
                'withPublicIp': sum(1 for v in vms if v.get('publicIps')),
            }
            if vms is not None
            else None
        )

        volumes = section('storage', lambda: zadara_resources.list_volumes(token))
        summary['storage'] = (
            {
                'volumes': len(volumes),
                'totalGiB': sum(v['sizeGiB'] for v in volumes),
                'attached': sum(1 for v in volumes if v['attachmentStatus'] == 'in-use'),
                'media': zadara_resources.media_totals(volumes),
            }
            if volumes is not None
            else None
        )

        vpcs = section('networks', lambda: zadara_resources.list_vpcs(token))
        summary['networks'] = {'vpcs': len(vpcs)} if vpcs is not None else None

        alarms = section('alarms', lambda: zadara_resources.list_alarms(token))
        summary['alarms'] = (
            {
                'open': sum(1 for a in alarms if a['state'] != 'closed'),
                'total': len(alarms),
            }
            if alarms is not None
            else None
        )

        return Response(
            {
                'data': summary,
                'meta': {
                    'project': request.session.get('zadara_project_name'),
                    'unavailable': unavailable,
                },
            }
        )


class DashboardMetricsView(APIView):
    """
    GET /api/v1/user/dashboard/metrics?period=… — utilisation of the whole
    current project, aggregated by the cloud rather than summed by us.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        token = vault.get(request.session.session_key) if request.session.session_key else None
        if not token:
            raise AppError(message='Session expired, please sign in again.', code='session_expired', status_code=401)

        project_id = request.session.get('zadara_project_id')
        if not project_id:
            raise AppError(
                message='This session has no project scope, so there is nothing to chart.',
                code='no_project_scope',
                status_code=409,
            )

        period = (request.query_params.get('period') or '24h').strip()

        try:
            metrics = zadara_resources.get_project_metrics(token, project_id, period)
        except ZadaraError as err:
            status = 400 if err.code == 'invalid_request' else 502
            raise AppError(message='Failed to load project utilisation', code=err.code, status_code=status)

        return Response({'data': metrics})
