"""
Admin endpoints (spec §4.3).

Reads go through the MSP service token, which can see the WHOLE cluster — 21
separate accounts share it. So every view here narrows the result to the
caller's own account before answering. The service token is an implementation
detail for reaching the data; it is never a licence to show it.
"""

from rest_framework.generics import ListAPIView
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.audit.services import record
from apps.authentication import vault
from apps.audit.serializers import AuditLogSerializer
from apps.common.exceptions import AppError
from apps.common.pagination import EnvelopePagination
from apps.common.permissions import IsAdmin
from apps.common.tenancy import account_domain as _account_domain
from apps.integrations.zadara import identity as zadara_identity
from apps.integrations.zadara import resources as zadara_resources
from apps.integrations.zadara import service as zadara_service
from apps.integrations.zadara.exceptions import ZadaraError


class AdminResourcesView(APIView):
    """
    GET /api/v1/admin/resources — every VM of the caller's OWN account.

    Optional ?project_id= narrows further, but only to a project of that same
    account: an id from someone else's account returns nothing rather than
    their machines. ADMIN only.
    """

    permission_classes = [IsAdmin]

    def get(self, request):
        account = (request.user.account or '').strip()
        if not account:
            raise AppError(
                message='Your session has no account, please sign in again.',
                code='session_expired',
                status_code=401,
            )

        try:
            token = zadara_service.get_service_token()
            domain_id = zadara_service.resolve_domain_id(account)
            if not domain_id:
                raise AppError(message='Account not found in the cloud.', code='account_not_found', status_code=404)

            projects = zadara_service.list_domain_projects(domain_id)  # {id: name}
            vms = zadara_resources.list_vms(token, with_disks=True)
        except ZadaraError as err:
            raise AppError(message='Failed to load account resources', code=err.code, status_code=502)

        requested = request.query_params.get('project_id')
        allowed = {requested} & set(projects) if requested else set(projects)

        vms = [v for v in vms if v.get('projectId') in allowed]
        for vm in vms:
            vm['projectName'] = projects.get(vm.get('projectId') or '', '')

        return Response(
            {
                'data': {'vms': vms},
                'meta': {
                    'total': len(vms),
                    'diskInfo': any(vm.get('diskGiB') is not None for vm in vms),
                    'account': account,
                    'projects': [{'id': pid, 'name': name} for pid, name in sorted(projects.items(), key=lambda p: p[1])],
                },
            }
        )


class AdminAuditView(ListAPIView):
    """
    GET /api/v1/admin/audit — what users of the caller's account did. ADMIN only.

    Filters: ?action=vm.start &username= &resource_id= &outcome=SUCCESS|FAILURE.
    """

    permission_classes = [IsAdmin]
    serializer_class = AuditLogSerializer
    pagination_class = EnvelopePagination

    def get_queryset(self):
        # Same rule as the resource view: an admin sees their own account only.
        entries = AuditLog.objects.filter(account=self.request.user.account or '')
        params = self.request.query_params

        for field in ('action', 'username', 'resource_id', 'outcome'):
            value = (params.get(field) or '').strip()
            if value:
                entries = entries.filter(**{field: value})

        return entries


class AdminUsersView(APIView):
    """
    GET /api/v1/admin/users — users of the caller's own account. ADMIN only.

    Cloud facts (name, e-mail, whether MFA is registered, password expiry) come
    from the cloud; what the cabinet knows on its own — role and last sign-in —
    is joined from our own user rows, so an admin can see who has actually used
    the cabinet and who only exists in the cloud.
    """

    permission_classes = [IsAdmin]

    def get(self, request):
        account, domain_id = _account_domain(request)

        try:
            users = zadara_service.list_domain_users(domain_id)
        except ZadaraError as err:
            raise AppError(message='Failed to load account users', code=err.code, status_code=502)

        known = {u.zadara_user_id: u for u in User.objects.filter(account__iexact=account)}
        for user in users:
            local = known.get(user['id'])
            user['appRole'] = local.app_role if local else None
            user['status'] = local.status if local else None
            user['lastSeenAt'] = local.last_seen_at if local else None

        users.sort(key=lambda u: (u['systemUser'], u['name'].lower()))

        return Response({'data': users, 'meta': {'total': len(users), 'account': account}})


class AdminTenantsView(APIView):
    """
    GET /api/v1/admin/tenants — projects of the caller's own account, with what
    each one is actually running. ADMIN only.
    """

    permission_classes = [IsAdmin]

    def get(self, request):
        account, domain_id = _account_domain(request)

        try:
            projects = zadara_service.list_domain_project_details(domain_id)
            vms = zadara_resources.list_vms(zadara_service.get_service_token())
        except ZadaraError as err:
            raise AppError(message='Failed to load account projects', code=err.code, status_code=502)

        owned = {p['id'] for p in projects}
        usage: dict[str, dict] = {pid: {'vms': 0, 'running': 0, 'vcpus': 0, 'ramMB': 0} for pid in owned}

        for vm in vms:
            pid = vm.get('projectId')
            if pid not in owned:
                continue  # another account's machine — not ours to count, let alone show
            bucket = usage[pid]
            bucket['vms'] += 1
            bucket['running'] += 1 if vm['status'].lower() in ('active', 'running') else 0
            bucket['vcpus'] += vm['vcpus']
            bucket['ramMB'] += vm['ramMB']

        for project in projects:
            project.update(usage[project['id']])

        projects.sort(key=lambda p: (-p['vms'], p['name'].lower()))

        return Response({'data': projects, 'meta': {'total': len(projects), 'account': account}})


class AdminQuotaView(APIView):
    """
    GET /api/v1/admin/quotas — the account's ceilings and which project is
    spending them. ADMIN only.

    The ceilings live on the account; consumption lives on each project, and the
    cloud will not add them up for us. So this reads the account document plus
    one document per project of that account — and nothing outside it: a project
    of another account is never fetched, let alone counted.

    Per project only the rows that carry usage or a limit are returned. All 35
    rows times every project is mostly zeroes, and a table of zeroes hides the
    handful of numbers an administrator came to see.
    """

    permission_classes = [IsAdmin]

    def get(self, request):
        account, domain_id = _account_domain(request)
        token = zadara_service.get_service_token()

        try:
            account_quotas = zadara_resources.list_domain_quotas(token, domain_id)
            projects = zadara_service.list_domain_projects(domain_id)  # {id: name}
        except ZadaraError as err:
            raise AppError(message='Failed to load account quotas', code=err.code, status_code=502)

        rows = []
        unavailable = []
        for project_id, name in sorted(projects.items(), key=lambda p: p[1].lower()):
            try:
                quotas = zadara_resources.list_project_quotas(token, project_id)
            except ZadaraError:
                unavailable.append(name)
                continue

            used = [q for q in quotas if q['allocated'] or not q['unlimited']]
            rows.append({'id': project_id, 'name': name, 'quotas': used})

        limited = [q for q in account_quotas if not q['unlimited']]

        return Response(
            {
                'data': {'account': account_quotas, 'projects': rows},
                'meta': {
                    'account': account,
                    'projects': len(rows),
                    'limitsSet': len(limited),
                    'nearLimit': sum(1 for q in limited if (q['usedPercent'] or 0) >= 80),
                    'unavailable': unavailable,
                },
            }
        )


def _admin_token(request) -> str:
    """The administrator's own token — writes are made with their rights."""
    token = vault.get(request.session.session_key) if request.session.session_key else None
    if not token:
        raise AppError(message='Session expired, please sign in again.', code='session_expired', status_code=401)

    return token


def _user_of_account(request, user_id: str) -> dict:
    """Resolve a user id inside the caller's own account, or refuse.

    The cloud would already stop a tenant admin from reaching another account,
    but a 404 here means we never even ask on behalf of the wrong account.
    """
    _, domain_id = _account_domain(request)

    try:
        users = zadara_service.list_domain_users(domain_id)
    except ZadaraError as err:
        raise AppError(message='Failed to reach the cloud', code=err.code, status_code=502)

    found = next((u for u in users if u['id'] == user_id), None)
    if not found:
        raise AppError(message='No such user in your account.', code='not_found', status_code=404)

    return found


_WRITE_STATUS = {'forbidden': 403, 'not_found': 404, 'conflict': 409, 'invalid_request': 400}


def _write_failed(err: ZadaraError, action: str, request, resource_id: str, resource_name: str):
    record(
        request,
        action,
        resource_type='user',
        resource_id=resource_id,
        resource_name=resource_name,
        outcome=AuditLog.FAILURE,
        error_code=err.code,
    )
    raise AppError(message=err.message, code=err.code, status_code=_WRITE_STATUS.get(err.code, 502))


class AdminUserCreateView(APIView):
    """POST /api/v1/admin/users — create a user in the caller's own account."""

    permission_classes = [IsAdmin]

    def post(self, request):
        _, domain_id = _account_domain(request)
        token = _admin_token(request)

        data = request.data
        name = (data.get('name') or '').strip()
        email = (data.get('email') or '').strip()
        password = data.get('password') or ''
        project_id = (data.get('projectId') or '').strip()
        must_change = data.get('mustChangePassword')
        must_change = True if must_change is None else bool(must_change)

        if not name or not password:
            raise AppError(message='A username and a password are required.', code='invalid_request', status_code=400)

        # A project id from someone else's account must not be smuggled in here.
        if project_id and project_id not in zadara_service.list_domain_projects(domain_id):
            raise AppError(message='No such project in your account.', code='invalid_request', status_code=400)

        try:
            created = zadara_identity.create_user(
                token,
                domain_id,
                name,
                email,
                password,
                must_change_password=must_change,
                project_id=project_id,
            )
        except ZadaraError as err:
            _write_failed(err, 'user.create', request, '', name)

        user_id = str((created or {}).get('id') or '')

        # Membership is a separate call; without it the new user can sign in but
        # sees nothing.
        role_granted = False
        if project_id and user_id:
            try:
                zadara_identity.grant_project_role(token, project_id, user_id)
                role_granted = True
            except ZadaraError as err:
                record(
                    request,
                    'user.grant_role',
                    resource_type='user',
                    resource_id=user_id,
                    resource_name=name,
                    outcome=AuditLog.FAILURE,
                    error_code=err.code,
                    detail={'projectId': project_id},
                )

        record(
            request,
            'user.create',
            resource_type='user',
            resource_id=user_id,
            resource_name=name,
            detail={'projectId': project_id, 'roleGranted': role_granted, 'mustChangePassword': must_change},
        )

        return Response({'data': created, 'meta': {'roleGranted': role_granted}}, status=201)


class AdminUserDetailView(APIView):
    """PATCH / DELETE /api/v1/admin/users/{id} — edit or remove one user."""

    permission_classes = [IsAdmin]

    def patch(self, request, user_id: str):
        target = _user_of_account(request, user_id)
        token = _admin_token(request)
        data = request.data

        changes = {}
        for field, key in (('name', 'name'), ('email', 'email')):
            if data.get(key) is not None:
                changes[field] = str(data[key]).strip()
        if data.get('enabled') is not None:
            changes['enabled'] = bool(data['enabled'])
        if data.get('password'):
            changes['password'] = data['password']
        if data.get('passwordNeverExpires') is not None:
            changes['password_never_expires'] = bool(data['passwordNeverExpires'])

        if not changes:
            raise AppError(message='Nothing to change.', code='invalid_request', status_code=400)

        # Disabling yourself locks you out of the cabinet you are standing in.
        if changes.get('enabled') is False and user_id == request.user.zadara_user_id:
            raise AppError(message='You cannot disable your own account.', code='invalid_request', status_code=400)

        try:
            updated = zadara_identity.update_user(token, user_id, **changes)
        except ZadaraError as err:
            _write_failed(err, 'user.update', request, user_id, target['name'])

        record(
            request,
            'user.update',
            resource_type='user',
            resource_id=user_id,
            resource_name=target['name'],
            # What changed, never the new secret itself.
            detail={'fields': sorted(k for k in changes if k != 'password'), 'passwordSet': 'password' in changes},
        )

        return Response({'data': updated})

    def delete(self, request, user_id: str):
        target = _user_of_account(request, user_id)
        token = _admin_token(request)

        if user_id == request.user.zadara_user_id:
            raise AppError(message='You cannot delete your own account.', code='invalid_request', status_code=400)

        try:
            zadara_identity.delete_user(token, user_id)
        except ZadaraError as err:
            _write_failed(err, 'user.delete', request, user_id, target['name'])

        record(request, 'user.delete', resource_type='user', resource_id=user_id, resource_name=target['name'])
        User.objects.filter(zadara_user_id=user_id).delete()

        return Response({'data': {'detail': f'User {target["name"]} was deleted.'}})
