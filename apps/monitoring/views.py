"""
Monitoring endpoints (spec §3.6): what the cloud itself recorded.

This is not the cabinet's audit log. `apps.audit` records what people did
*through us*; these events are the cloud's own — logins, snapshots, load
balancer reconfigurations, failures inside the platform — and they exist
whether the cabinet was involved or not. Both are needed, and they answer
different questions.

Every read is pinned to the session's project. Unfiltered, the events API hands
a wide token the whole cluster's stream (thousands of rows across every
account), so the project id is not a filter the client may choose.
"""

import collections

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.authentication import vault
from apps.common.exceptions import AppError
from apps.integrations.zadara import resources as zadara_resources
from apps.integrations.zadara.exceptions import ZadaraError

PAGE_SIZE_MAX = 200


def _token(request) -> str:
    token = vault.get(request.session.session_key) if request.session.session_key else None
    if not token:
        raise AppError(message='Session expired, please sign in again.', code='session_expired', status_code=401)

    return token


class EventListView(APIView):
    """
    GET /api/v1/user/events — the cloud's log for the current project.

    Query: ?period=1h|24h|7d|30d &severity=INFO|WARNING|ERROR &entityType=
           &entityId= &eventType= &search= &limit= &offset=

    The upstream API reports no total, so the whole window is fetched once and
    counted here; the response carries the severity breakdown and the real
    total, which a bare page of rows could never state.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        token = _token(request)

        project_id = request.session.get('zadara_project_id')
        if not project_id:
            raise AppError(
                message='This session has no project scope, so there is no event log to show.',
                code='no_project_scope',
                status_code=409,
            )

        params = request.query_params

        try:
            limit = min(int(params.get('limit') or 50), PAGE_SIZE_MAX)
            offset = max(int(params.get('offset') or 0), 0)
        except ValueError:
            raise AppError(message='limit and offset must be numbers', code='invalid_request', status_code=400)

        try:
            window = zadara_resources.list_events(
                token,
                project_id=project_id,
                period=(params.get('period') or '24h').strip(),
                severity=params.get('severity') or None,
                entity_type=params.get('entityType') or None,
                entity_id=params.get('entityId') or None,
                event_type=params.get('eventType') or None,
            )
        except ZadaraError as err:
            if err.code == 'invalid_request':
                raise AppError(message=err.message, code=err.code, status_code=400)
            if err.code == 'forbidden':
                raise AppError(
                    message='Your account cannot read the cloud event log.',
                    code='forbidden',
                    status_code=403,
                )
            raise AppError(message='Failed to load events', code=err.code, status_code=502)

        events = window['events']

        # Free-text match happens here rather than upstream: the API filters by
        # id and type but has nothing that searches a description.
        search = (params.get('search') or '').strip().lower()
        if search:
            events = [
                e
                for e in events
                if search in e['description'].lower()
                or search in e['typeName'].lower()
                or search in e['entityName'].lower()
            ]

        severities = collections.Counter(e['severity'] for e in events)
        types = collections.Counter((e['typeId'], e['typeName']) for e in events)
        entity_types = sorted({e['entityType'] for e in events if e['entityType']})

        return Response(
            {
                'data': events[offset : offset + limit],
                'meta': {
                    'total': len(events),
                    'limit': limit,
                    'offset': offset,
                    'period': window['period'],
                    'from': window['from'],
                    'to': window['to'],
                    'truncated': window['truncated'],
                    'project': request.session.get('zadara_project_name'),
                    'severities': {level: severities.get(level, 0) for level in zadara_resources.EVENT_SEVERITIES},
                    'entityTypes': entity_types,
                    'topTypes': [
                        {'id': type_id, 'name': name, 'count': count}
                        for (type_id, name), count in types.most_common(8)
                    ],
                },
            }
        )


class AlarmListView(APIView):
    """
    GET /api/v1/user/alarms — alarms the cloud raised, as the caller's token
    sees them.

    On this deployment alarms are platform-level (nodes, storage pools, address
    pools) and carry no project id, so they read as cloud health rather than as
    something a tenant caused. They are shown as such — the dashboard already
    counts the open ones.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        token = _token(request)

        try:
            alarms = zadara_resources.list_alarms(token)
        except ZadaraError as err:
            if err.code == 'forbidden':
                raise AppError(
                    message='Your account cannot read cloud alarms.',
                    code='forbidden',
                    status_code=403,
                )
            raise AppError(message='Failed to load alarms', code=err.code, status_code=502)

        # Open first, newest first inside each group.
        alarms.sort(key=lambda a: a['createdAt'] or '', reverse=True)
        alarms.sort(key=lambda a: a['state'] == 'closed')
        open_alarms = [a for a in alarms if a['state'] != 'closed']

        return Response(
            {
                'data': alarms,
                'meta': {
                    'total': len(alarms),
                    'open': len(open_alarms),
                    'entityTypes': sorted({a['entityType'] for a in alarms if a['entityType']}),
                },
            }
        )
