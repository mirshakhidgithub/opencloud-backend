"""
Quota endpoints (spec §3.7): how much of the account's allowance is used.

The cloud keeps the ceilings on the ACCOUNT and the consumption on each
PROJECT, so a useful page needs both: the project answers "what am I using",
the account answers "how close are we to the limit". Both are read with the
caller's own token — the service token is not used here, because a user's right
to see their own allowance is the cloud's decision, not ours.
"""

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.authentication import vault
from apps.common.concurrency import gather
from apps.common.exceptions import AppError
from apps.integrations.zadara import resources as zadara_resources
from apps.integrations.zadara import service as zadara_service


class UserQuotaView(APIView):
    """
    GET /api/v1/user/quotas — the current project's usage next to the account's
    ceilings.

    Each half is fetched independently: a member who may not read the account
    document still gets their project numbers, and the missing half is named in
    `meta.unavailable` instead of blanking the page.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        token = vault.get(request.session.session_key) if request.session.session_key else None
        if not token:
            raise AppError(message='Session expired, please sign in again.', code='session_expired', status_code=401)

        project_id = request.session.get('zadara_project_id')
        account = (request.user.account or '').strip()
        unavailable = []

        # The account's quota document needs its domain id first, so that pair
        # stays sequential; the project's document does not depend on either and
        # runs alongside them.
        def account_half():
            domain_id = zadara_service.resolve_domain_id(account) if account else None

            return zadara_resources.list_domain_quotas(token, domain_id) if domain_id else None

        sources = {'account': account_half}
        if project_id:
            sources['project'] = lambda: zadara_resources.list_project_quotas(token, project_id)
        else:
            unavailable.append('project')

        results = gather(sources)

        project = results['project'].value if 'project' in results else None
        account_quotas = results['account'].value
        unavailable += [name for name, result in results.items() if not result.ok or result.value is None]

        if project is None and account_quotas is None:
            raise AppError(message='Failed to load quotas', code='upstream_error', status_code=502)

        # Only the rows with a real ceiling can be "close to full" — the rest
        # are unlimited on this cluster and must not be reported as pressure.
        limited = [q for q in (account_quotas or []) if not q['unlimited']]

        return Response(
            {
                'data': {
                    'project': project,
                    'account': account_quotas,
                },
                'meta': {
                    'account': account,
                    'project': request.session.get('zadara_project_name'),
                    'limitsSet': len(limited),
                    'nearLimit': sum(1 for q in limited if (q['usedPercent'] or 0) >= 80),
                    'unavailable': sorted(set(unavailable)),
                },
            }
        )
