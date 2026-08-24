"""Storage endpoints (spec §3.4): the volumes of the current project."""

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.authentication import vault
from apps.common.concurrency import gather
from apps.common.exceptions import AppError
from apps.integrations.zadara import resources as zadara_resources


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

        # The machine names are only a column, but fetching them after the
        # volumes cost a whole extra round-trip — so both go at once. A volume
        # list without machine names still beats an error.
        results = gather(
            {
                'volumes': lambda: zadara_resources.list_volumes(token),
                'machines': lambda: zadara_resources.list_vms(token),
            }
        )

        if not results['volumes'].ok:
            code = getattr(results['volumes'].error, 'code', 'upstream_error')
            if code == 'forbidden':
                raise AppError(
                    message='Your account cannot list volumes in this project.',
                    code='forbidden',
                    status_code=403,
                )
            raise AppError(message='Failed to load volumes', code=code, status_code=502)

        volumes = results['volumes'].value
        names = {vm['id']: vm['name'] for vm in results['machines'].value or []}

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

        # Both snapshot kinds and the volumes that name them: three unrelated
        # reads, so one wave instead of three.
        results = gather(
            {
                'volumes': lambda: zadara_resources.list_volume_snapshots(token),
                'machines': lambda: zadara_resources.list_vm_snapshots(token),
                'sources': lambda: zadara_resources.list_volumes(token),
            }
        )

        def code_of(name):
            return None if results[name].ok else getattr(results[name].error, 'code', 'upstream_error')

        volume_snapshots = results['volumes'].value or []
        vm_snapshots = results['machines'].value or []
        volume_error = code_of('volumes')
        vm_error = code_of('machines')

        if volume_error and vm_error:
            raise AppError(message='Failed to load snapshots', code=volume_error, status_code=502)

        # Label each volume snapshot with the volume it came from, where that
        # volume still exists — snapshots outlive their source. A snapshot
        # carries no volume type of its own, so the medium is inherited from
        # that source and stays blank once the source is gone.
        sources = {v['id']: v for v in results['sources'].value or []}

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
