"""
Taking the daily measurement.

One pass covers the whole cluster: four list calls with the service token, then
grouping by `project_id`. That is deliberately cheaper than walking accounts —
per-account it would be four calls times twenty-one accounts for the same
numbers.

Every project of every account is sampled, including accounts that never signed
into the cabinet: a bill has to cover the month, and an account whose first
login happens in week three still consumed the first two weeks. The API layer,
not the collector, is where the account boundary is enforced.
"""

import logging
from datetime import date, datetime, timezone

from apps.integrations.zadara import resources as zadara_resources
from apps.integrations.zadara import service as zadara_service
from apps.integrations.zadara.exceptions import ZadaraError

from .models import UsageSnapshot

logger = logging.getLogger('billing')

RUNNING = ('active', 'running')


def _blank() -> dict:
    return {
        'vms_total': 0,
        'vms_running': 0,
        'vcpus': 0,
        'ram_mb': 0,
        'ssd_gib': 0,
        'hdd_gib': 0,
        'unlabelled_gib': 0,
        'elastic_ips': 0,
        'snapshot_gib': 0,
    }


def measure_cluster(token: str) -> dict[str, dict]:
    """project_id → the day's measurements, for every project holding anything.

    Each source is optional: losing the snapshot list must not throw away the
    machines and disks we did read, because a missing day cannot be recovered.
    """
    buckets: dict[str, dict] = {}

    def bucket(project_id: str | None) -> dict | None:
        # A resource with no project cannot be billed to anyone; counting it
        # somewhere would be worse than not counting it.
        return buckets.setdefault(project_id, _blank()) if project_id else None

    def source(name, fetch):
        try:
            return fetch()
        except ZadaraError as err:
            logger.warning('usage snapshot: %s unavailable (%s)', name, err.code)

            return None

    for vm in source('machines', lambda: zadara_resources.list_vms(token)) or []:
        entry = bucket(vm.get('projectId'))
        if entry is None:
            continue

        entry['vms_total'] += 1
        if vm['status'].lower() in RUNNING:
            entry['vms_running'] += 1
            # Compute is charged only while the machine runs; its disks are
            # charged either way and counted from the volume list below.
            entry['vcpus'] += vm['vcpus']
            entry['ram_mb'] += vm['ramMB']

    for volume in source('volumes', lambda: zadara_resources.list_volumes(token)) or []:
        entry = bucket(volume.get('projectId'))
        if entry is None:
            continue

        key = {'SSD': 'ssd_gib', 'HDD': 'hdd_gib'}.get(volume.get('media') or '', 'unlabelled_gib')
        entry[key] += volume['sizeGiB']

    for eip in source('elastic addresses', lambda: zadara_resources.list_elastic_ips(token)) or []:
        entry = bucket(eip.get('projectId'))
        if entry is not None:
            entry['elastic_ips'] += 1

    for snapshot in source('snapshots', lambda: zadara_resources.list_volume_snapshots(token)) or []:
        entry = bucket(snapshot.get('projectId'))
        if entry is not None:
            entry['snapshot_gib'] += snapshot['sizeGiB']

    return buckets


def project_directory() -> dict[str, dict]:
    """project_id → {account, domain_id, name} across every account."""
    directory: dict[str, dict] = {}

    for domain in zadara_service.list_domains():
        try:
            projects = zadara_service.list_domain_projects(domain['id'])
        except ZadaraError as err:
            logger.warning('usage snapshot: projects of %s unavailable (%s)', domain['name'], err.code)
            continue

        for project_id, name in projects.items():
            directory[project_id] = {'account': domain['name'], 'domain_id': domain['id'], 'name': name}

    return directory


def capture(taken_on: date | None = None) -> dict:
    """Write one row per project for the given day. Re-running a day overwrites it."""
    now = datetime.now(timezone.utc)
    taken_on = taken_on or now.date()

    token = zadara_service.get_service_token()
    measurements = measure_cluster(token)
    directory = project_directory()

    written = 0
    unknown = 0
    for project_id, values in measurements.items():
        known = directory.get(project_id)
        if not known:
            # A project the service account cannot see the owner of: recorded
            # anyway, with no account, so the figures are not silently dropped.
            unknown += 1

        UsageSnapshot.objects.update_or_create(
            project_id=project_id,
            taken_on=taken_on,
            defaults={
                'taken_at': now,
                'account': known['account'] if known else '',
                'domain_id': known['domain_id'] if known else '',
                'project_name': known['name'] if known else '',
                **values,
            },
        )
        written += 1

    logger.info('usage snapshot for %s: %s projects (%s without an account)', taken_on, written, unknown)

    return {'date': str(taken_on), 'projects': written, 'unattributed': unknown}
