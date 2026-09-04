"""
Platform-wide endpoints (`/api/v1/platform/*`).

The difference from `apps.admin_api` is the whole point of this app: those views
hold a cluster-wide service token and then deliberately narrow the answer to the
caller's own account. These do not narrow. That is safe only because nothing
here is reachable without a `PlatformAdmin` session, which no Zadara role can
grant and no cabinet cookie can satisfy.
"""

import time

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.db.models import Count, Q
from rest_framework.response import Response

from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.audit.serializers import AuditLogSerializer
from apps.common.exceptions import AppError
from apps.common.pagination import EnvelopePagination
from apps.integrations.zadara import identity as zadara_identity
from apps.integrations.zadara import service as zadara_service
from apps.integrations.zadara.exceptions import ZadaraError

from . import services, snapshot
from .authentication import PlatformAPIView, PlatformListAPIView
from .models import AdminAction
from .permissions import CanWritePlatform, IsPlatformAdmin
from .serializers import AdminActionSerializer


def _snapshot_meta(snap: dict) -> dict:
    """What every snapshot-backed screen has to show alongside the numbers."""
    return {'builtAt': snap['builtAt'], 'stale': snap['stale'], 'buildSeconds': snap['buildSeconds']}


def _wants_refresh(request) -> bool:
    return request.query_params.get('refresh') in {'1', 'true', 'yes'}


class AccountsView(PlatformAPIView):
    """GET /platform/accounts — every account on the cluster, with its size."""

    permission_classes = [IsPlatformAdmin]

    def get(self, request):
        snap = snapshot.get(refresh=_wants_refresh(request))

        # How many cabinet users each account has signed in with. Distinct from
        # the Keystone user count on the account card: this counts the people who
        # actually use the console, which is the number worth watching.
        cabinet_users = _cabinet_user_counts()

        accounts = [
            {
                **{k: v for k, v in a.items() if k != 'projects'},
                'projectCount': len(a['projects']),
                'cabinetUsers': cabinet_users.get(a['name'].lower(), 0),
            }
            for a in snap['accounts']
        ]

        return Response(
            {
                'data': accounts,
                'meta': {**_snapshot_meta(snap), 'total': len(accounts), 'totals': snap['totals']},
            }
        )


def _cabinet_user_counts() -> dict[str, int]:
    rows = User.objects.values('account').annotate(n=Count('id'))

    return {(r['account'] or '').lower(): r['n'] for r in rows}


class AccountDetailView(PlatformAPIView):
    """GET /platform/accounts/<id> — one account: projects, users, footprint."""

    permission_classes = [IsPlatformAdmin]

    def get(self, request, account_id: str):
        entry = snapshot.account(account_id)
        if entry is None:
            raise AppError(message='No such account.', code='account_not_found', status_code=404)

        try:
            users = _account_users(account_id)
        except ZadaraError:
            raise AppError(message='Could not read the account users.', code='upstream_error', status_code=502)

        # Cabinet-side facts Zadara does not have: who is blocked here, and who
        # has ever actually signed in.
        local = {u.username.lower(): u for u in User.objects.filter(account__iexact=entry['name'])}
        for user in users:
            row = local.get((user['name'] or '').lower())
            user['cabinetStatus'] = row.status if row else None
            user['lastSeenAt'] = row.last_seen_at.isoformat() if row else None
            user['appRole'] = row.app_role if row else None

        return Response({'data': {**entry, 'users': users}})


class UsersView(PlatformAPIView):
    """GET /platform/users — users across every account, in one list."""

    permission_classes = [IsPlatformAdmin]

    def get(self, request):
        snap = snapshot.get()
        wanted = (request.query_params.get('account') or '').strip().lower()
        query = (request.query_params.get('q') or '').strip().lower()

        accounts = [a for a in snap['accounts'] if not wanted or a['name'].lower() == wanted]

        rows = []
        unreadable = []
        for entry in accounts:
            try:
                users = _account_users(entry['id'])
            except ZadaraError:
                # One account failing must not blank the list; say which.
                unreadable.append(entry['name'])
                continue
            for user in users:
                rows.append({**user, 'account': entry['name'], 'accountId': entry['id']})

        local = {
            (u.account or '').lower() + '/' + (u.username or '').lower(): u
            for u in User.objects.all()
        }
        for row in rows:
            match = local.get(row['account'].lower() + '/' + (row['name'] or '').lower())
            row['cabinetStatus'] = match.status if match else None
            row['lastSeenAt'] = match.last_seen_at.isoformat() if match else None

        if query:
            rows = [
                r for r in rows
                if query in (r['name'] or '').lower()
                or query in (r.get('email') or '').lower()
                or query in r['account'].lower()
            ]

        rows.sort(key=lambda r: (r['account'].lower(), (r['name'] or '').lower()))

        return Response(
            {'data': rows, 'meta': {'total': len(rows), 'unreadableAccounts': unreadable}}
        )


# Zadara has no cross-account user endpoint, so the unfiltered list is one call
# per account — 21 of them, in sequence, on every page load. Cached per account
# for a minute: short enough that a switch flipped here shows up on the next
# look, long enough that paging back and forth does not re-scan the cluster.
_USERS_CACHE_TTL = 60


def _account_users(account_id: str) -> list[dict]:
    key = f'platform_account_users:{account_id}'
    users = cache.get(key)
    if users is None:
        users = zadara_service.list_domain_users(account_id)
        cache.set(key, users, _USERS_CACHE_TTL)

    return users


def _forget_account_users(account_id: str) -> None:
    """After a write, the cached copy is a lie — drop it."""
    cache.delete(f'platform_account_users:{account_id}')


class UserDetailView(PlatformAPIView):
    """
    PATCH /platform/users/<id> — turn a user off, or back on.

    Two switches, and they are not the same thing:

    * `enabled`       — the Keystone user. Off means the credentials stop working
                        everywhere, including the Zadara API and the CLI.
    * `cabinetStatus` — our local row. Blocked means the console refuses the
                        sign-in, while the same credentials still work against
                        Zadara directly.

    Disabling the console alone and calling someone "cut off" is the mistake this
    endpoint exists to make hard, so the panel offers both and names them.
    """

    permission_classes = [CanWritePlatform]

    def patch(self, request, user_id: str):
        account_name = (request.data.get('account') or '').strip()
        if not account_name:
            raise AppError(message='`account` is required.', code='invalid_request', status_code=400)

        enabled = request.data.get('enabled')
        cabinet_status = request.data.get('cabinetStatus')

        if enabled is None and cabinet_status is None:
            raise AppError(message='Nothing to change.', code='invalid_request', status_code=400)

        changed = {}

        if enabled is not None:
            try:
                token = zadara_service.get_service_token()
                zadara_identity.update_user(token, user_id, enabled=bool(enabled))
            except ZadaraError as err:
                services.record(
                    request, 'user.update',
                    target_account=account_name, target_type='user', target_id=user_id,
                    outcome=AdminAction.FAILURE, error_code=err.code,
                    detail={'attempted': {'enabled': bool(enabled)}},
                )
                raise AppError(message='The cloud refused the change.', code=err.code, status_code=502)
            changed['enabled'] = bool(enabled)

        if cabinet_status is not None:
            if cabinet_status not in {User.STATUS_ACTIVE, User.STATUS_BLOCKED}:
                raise AppError(message='Unknown cabinet status.', code='invalid_request', status_code=400)
            # Only touches people who have signed in at least once — there is no
            # local row to block before that, and creating one would invent a
            # user the console has never seen.
            User.objects.filter(account__iexact=account_name, zadara_user_id=user_id).update(status=cabinet_status)
            changed['cabinetStatus'] = cabinet_status

        domain_id = zadara_service.resolve_domain_id(account_name)
        if domain_id:
            _forget_account_users(domain_id)

        services.record(
            request,
            'user.update',
            target_account=account_name,
            target_type='user',
            target_id=user_id,
            detail=changed,
        )

        return Response({'data': {'id': user_id, **changed}})


class ResourcesView(PlatformAPIView):
    """
    GET /platform/resources — what the cluster is carrying, and for whom.

    A bare sum of vCPU is not a number anyone acts on. What makes it actionable
    is the same figure against what the hardware has, so the response carries the
    configured capacity next to the usage and the ratio between them.
    """

    permission_classes = [IsPlatformAdmin]

    def get(self, request):
        snap = snapshot.get(refresh=_wants_refresh(request))
        totals = snap['totals']
        capacity = getattr(settings, 'PLATFORM_CAPACITY', {}) or {}

        return Response(
            {
                'data': {
                    'totals': totals,
                    'capacity': capacity,
                    'utilisation': _utilisation(totals, capacity),
                    'byAccount': [
                        {
                            'id': a['id'],
                            'name': a['name'],
                            'vmCount': a['vmCount'],
                            'runningVms': a['runningVms'],
                            'vcpus': a['vcpus'],
                            'ramMB': a['ramMB'],
                            'diskGB': a['diskGB'],
                        }
                        for a in snap['accounts']
                    ],
                    'unattributedVms': snap['unattributedVms'],
                },
                'meta': _snapshot_meta(snap),
            }
        )


def _utilisation(totals: dict, capacity: dict) -> dict:
    """Allocated ÷ installed, per dimension. Absent where capacity is unset —
    a made-up denominator is worse than no percentage at all."""
    out = {}
    for key, installed in capacity.items():
        used = totals.get(key)
        if not installed or used is None:
            continue
        out[key] = {'used': used, 'total': installed, 'percent': round(used / installed * 100, 1)}
    return out


class HealthView(PlatformAPIView):
    """GET /platform/health — is the platform itself all right?"""

    permission_classes = [IsPlatformAdmin]

    def get(self, request):
        return Response({'data': _health()})


def _health() -> dict:
    checks = []

    started = time.monotonic()
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
        checks.append(_ok('database', started))
    except Exception as err:  # noqa: BLE001 - a health check reports, never raises
        checks.append(_fail('database', started, err))

    started = time.monotonic()
    try:
        cache.set('platform_health_probe', 1, 10)
        ok = cache.get('platform_health_probe') == 1
        checks.append(_ok('cache', started) if ok else _fail('cache', started, 'value did not come back'))
    except Exception as err:  # noqa: BLE001
        checks.append(_fail('cache', started, err))

    started = time.monotonic()
    try:
        zadara_service.get_service_token()
        checks.append(_ok('zadara', started))
    except Exception as err:  # noqa: BLE001
        checks.append(_fail('zadara', started, err))

    snap = None
    try:
        snap = snapshot.get()
    except ZadaraError:
        pass

    return {
        'status': 'ok' if all(c['ok'] for c in checks) else 'degraded',
        'checks': checks,
        'snapshot': _snapshot_meta(snap) if snap else None,
    }


def _ok(name: str, started: float) -> dict:
    return {'name': name, 'ok': True, 'ms': round((time.monotonic() - started) * 1000)}


def _fail(name: str, started: float, err) -> dict:
    return {'name': name, 'ok': False, 'ms': round((time.monotonic() - started) * 1000), 'error': str(err)[:200]}


class AuditView(PlatformListAPIView):
    """GET /platform/audit — what cabinet users did, across every account."""

    permission_classes = [IsPlatformAdmin]
    serializer_class = AuditLogSerializer
    pagination_class = EnvelopePagination

    def get_queryset(self):
        qs = AuditLog.objects.all()
        params = self.request.query_params

        if account := params.get('account'):
            qs = qs.filter(account__iexact=account)
        if action := params.get('action'):
            qs = qs.filter(action=action)
        if username := params.get('username'):
            qs = qs.filter(username__icontains=username)
        if outcome := params.get('outcome'):
            qs = qs.filter(outcome=outcome)
        if search := params.get('q'):
            qs = qs.filter(
                Q(username__icontains=search)
                | Q(resource_name__icontains=search)
                | Q(account__icontains=search)
            )

        return qs


class ActivityView(PlatformListAPIView):
    """GET /platform/activity — what the operators themselves did."""

    permission_classes = [IsPlatformAdmin]
    serializer_class = AdminActionSerializer
    pagination_class = EnvelopePagination

    def get_queryset(self):
        qs = AdminAction.objects.all()
        params = self.request.query_params

        if actor := params.get('actor'):
            qs = qs.filter(actor_email__icontains=actor)
        if action := params.get('action'):
            qs = qs.filter(action=action)
        if outcome := params.get('outcome'):
            qs = qs.filter(outcome=outcome)

        return qs
