"""Storage endpoints (spec §3.4): the volumes of the current project."""

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.authentication import vault
from apps.common.exceptions import AppError
from apps.integrations.zadara import resources as zadara_resources
from apps.integrations.zadara.exceptions import ZadaraError


class VolumeListView(APIView):
    """
    GET /api/v1/user/volumes — volumes visible to the current project, each
    labelled with the machine it is attached to.

    The machine names are joined here rather than in the browser: the client
    would otherwise have to fetch every VM just to render a column, and a user
    who cannot list machines should still see their volumes.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        token = vault.get(request.session.session_key) if request.session.session_key else None
        if not token:
            raise AppError(message='Session expired, please sign in again.', code='session_expired', status_code=401)

        try:
            volumes = zadara_resources.list_volumes(token)
        except ZadaraError as err:
            if err.code == 'forbidden':
                raise AppError(
                    message='Your account cannot list volumes in this project.',
                    code='forbidden',
                    status_code=403,
                )
            raise AppError(message='Failed to load volumes', code=err.code, status_code=502)

        try:
            names = {vm['id']: vm['name'] for vm in zadara_resources.list_vms(token)}
        except ZadaraError:
            names = {}  # a volume list without machine names still beats an error

        for volume in volumes:
            volume['attachedToName'] = names.get(volume['attachedToId'] or '', '')

        attached = sum(1 for v in volumes if v['attachmentStatus'] == 'in-use')

        return Response(
            {
                'data': volumes,
                'meta': {
                    'total': len(volumes),
                    'totalGiB': sum(v['sizeGiB'] for v in volumes),
                    'attached': attached,
                    'unattached': len(volumes) - attached,
                    # Capacity per medium: the same terabyte costs differently
                    # on SSD and HDD, so the split is the number people want.
                    'media': zadara_resources.media_totals(volumes),
                },
            }
        )


class SnapshotListView(APIView):
    """
    GET /api/v1/user/snapshots — both kinds this cloud keeps.

    Volume snapshots copy one volume; machine snapshots copy every volume of a
    VM at one moment. They live in different APIs and have different fields, so
    they are returned as two lists rather than forced into one shape.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        token = vault.get(request.session.session_key) if request.session.session_key else None
        if not token:
            raise AppError(message='Session expired, please sign in again.', code='session_expired', status_code=401)

        def safe(fetch):
            try:
                return fetch(), None
            except ZadaraError as err:
                return [], err.code

        volume_snapshots, volume_error = safe(lambda: zadara_resources.list_volume_snapshots(token))
        vm_snapshots, vm_error = safe(lambda: zadara_resources.list_vm_snapshots(token))

        if volume_error and vm_error:
            raise AppError(message='Failed to load snapshots', code=volume_error, status_code=502)

        # Label each volume snapshot with the volume it came from, where that
        # volume still exists — snapshots outlive their source. A snapshot
        # carries no volume type of its own, so the medium is inherited from
        # that source and stays blank once the source is gone.
        try:
            sources = {v['id']: v for v in zadara_resources.list_volumes(token)}
        except ZadaraError:
            sources = {}

        for snapshot in volume_snapshots:
            source = sources.get(snapshot['sourceVolumeId'] or '')
            snapshot['sourceVolumeName'] = source['name'] if source else ''
            snapshot['media'] = source['media'] if source else ''
            snapshot['volumeType'] = source['volumeType'] if source else ''

        return Response(
            {
                'data': {'volumeSnapshots': volume_snapshots, 'vmSnapshots': vm_snapshots},
                'meta': {
                    'volumeSnapshots': len(volume_snapshots),
                    'vmSnapshots': len(vm_snapshots),
                    'volumeSnapshotGiB': sum(s['sizeGiB'] for s in volume_snapshots),
                    'unavailable': [name for name, code in (('volumes', volume_error), ('machines', vm_error)) if code],
                },
            }
        )
