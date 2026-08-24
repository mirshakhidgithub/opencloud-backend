"""Compute endpoints (spec §3.3): user VM listing, detail and actions."""

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.models import AuditLog
from apps.audit.services import record
from apps.authentication import vault
from apps.common.exceptions import AppError
from apps.integrations.zadara import resources as zadara_resources
from apps.integrations.zadara.exceptions import ZadaraError

# Zadara error code → the status we answer with.
_STATUS_BY_CODE = {
    'forbidden': 403,
    'console_forbidden': 403,
    'console_unavailable': 502,
    'not_found': 404,
    'conflict': 409,
    'invalid_request': 400,
    'invalid_credentials': 401,
}


def _token(request) -> str:
    token = vault.get(request.session.session_key) if request.session.session_key else None
    if not token:
        raise AppError(message='Session expired, please sign in again.', code='session_expired', status_code=401)

    return token


def _fail(err: ZadaraError, fallback: str):
    raise AppError(
        message=err.message or fallback,
        code=err.code,
        status_code=_STATUS_BY_CODE.get(err.code, 502),
    )


class VmListView(APIView):
    """GET /api/v1/user/vms — VMs of the user's current project."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        token = _token(request)

        try:
            # with_disks: the list shows the storage each machine uses and the
            # medium under it, which lives on the volumes rather than the VM.
            vms = zadara_resources.list_vms(token, with_disks=True)
        except ZadaraError as err:
            if err.code == 'forbidden':
                raise AppError(
                    message='No project-scoped access to virtual machines.',
                    code='forbidden',
                    status_code=403,
                )
            raise AppError(message='Failed to load virtual machines', code=err.code, status_code=502)

        return Response(
            {
                'data': vms,
                'meta': {
                    'total': len(vms),
                    # False when the volume list was refused, so the client can
                    # fall back to the root-disk size instead of showing blanks.
                    'diskInfo': any(vm.get('diskGiB') is not None for vm in vms),
                },
            }
        )


class VmDetailView(APIView):
    """GET /api/v1/user/vms/{id} — one machine, with the details a card needs."""

    permission_classes = [IsAuthenticated]

    def get(self, request, vm_id: str):
        token = _token(request)

        try:
            vm = zadara_resources.get_vm(token, vm_id)
        except ZadaraError as err:
            _fail(err, 'Failed to load the virtual machine')

        return Response({'data': vm})


class VmActionView(APIView):
    """POST /api/v1/user/vms/{id}/actions — start, stop or reboot a machine.

    Every attempt is audited, successful or not: this is the first endpoint in
    the cabinet that changes something in the cloud.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, vm_id: str):
        token = _token(request)
        action = (request.data.get('action') or '').strip().lower()
        force = bool(request.data.get('force'))

        if action not in zadara_resources.VM_ACTIONS:
            raise AppError(
                message=f'Unknown action. Expected one of: {", ".join(zadara_resources.VM_ACTIONS)}.',
                code='invalid_request',
                status_code=400,
            )

        # Named before the action so the audit entry is readable even if the
        # machine disappears right after.
        name = (request.data.get('name') or '').strip()

        try:
            zadara_resources.vm_action(token, vm_id, action, force=force)
        except ZadaraError as err:
            record(
                request,
                f'vm.{action}',
                resource_type='vm',
                resource_id=vm_id,
                resource_name=name,
                outcome=AuditLog.FAILURE,
                error_code=err.code,
                detail={'force': force} if action == 'stop' else {},
            )
            _fail(err, 'The cloud refused this action')

        record(
            request,
            f'vm.{action}',
            resource_type='vm',
            resource_id=vm_id,
            resource_name=name,
            detail={'force': force} if action == 'stop' else {},
        )

        # The cloud applies the change asynchronously; the fresh state is what
        # the client should show while it settles.
        try:
            vm = zadara_resources.get_vm(token, vm_id)
        except ZadaraError:
            vm = {'id': vm_id, 'status': 'unknown'}

        return Response({'data': vm, 'meta': {'action': action}})


class VmConsoleView(APIView):
    """POST /api/v1/user/vms/{id}/console — open a VNC console session.

    A POST, not a GET: it makes the cloud hand out a session. Audited for the
    same reason — console access is as good as sitting at the machine.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, vm_id: str):
        token = _token(request)
        name = (request.data.get('name') or '').strip()

        try:
            session = zadara_resources.get_vm_console(token, vm_id)
        except ZadaraError as err:
            record(
                request,
                'vm.console',
                resource_type='vm',
                resource_id=vm_id,
                resource_name=name,
                outcome=AuditLog.FAILURE,
                error_code=err.code,
            )

            if err.code == 'console_forbidden':
                raise AppError(
                    message='Console access is not enabled for this machine. '
                    'The project owner has to allow it in the cloud console.',
                    code='console_forbidden',
                    status_code=403,
                )
            _fail(err, 'Could not open the console')

        record(request, 'vm.console', resource_type='vm', resource_id=vm_id, resource_name=name)

        return Response({'data': session})


class VmMetricsView(APIView):
    """
    GET /api/v1/user/vms/{id}/metrics?period=1h|6h|24h|7d|30d — utilisation
    history for one machine, read with the user's own token.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, vm_id: str):
        token = _token(request)
        period = (request.query_params.get('period') or '24h').strip()

        try:
            metrics = zadara_resources.get_vm_metrics(token, vm_id, period)
        except ZadaraError as err:
            _fail(err, 'Failed to load utilisation history')

        return Response({'data': metrics})
