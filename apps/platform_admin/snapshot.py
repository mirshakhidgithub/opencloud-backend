"""
One picture of the whole cluster, assembled once and shared.

Every screen in the admin panel wants a slightly different cut of the same
facts: the account registry wants counts per account, the capacity page wants
totals, the account card wants one account's detail. Asking Zadara separately
for each would fan a single page open into dozens of upstream calls across 21
accounts, which is exactly what the cabinet was careful not to do.

So it is built once — each resource list is a single service-token call that
already spans the cluster — and cached briefly. Screens read the snapshot;
nothing here is authoritative, and `stale` says so when Zadara could not be
reached and the last good picture is being served instead.

**What a client consumes is more than its machines.** Volumes, snapshots, public
addresses and networks are billable, finite things too, and a panel that counted
only vCPU would let a tenant with two idle VMs and forty terabytes of snapshots
look small. Every list the service token can read cluster-wide is folded in
here, attributed by the `projectId` each row carries.
"""

import logging
import time

from django.core.cache import cache

from apps.accounts import backups
from apps.common.concurrency import gather

from apps.integrations.zadara import resources as zadara_resources
from apps.integrations.zadara import service as zadara_service
from apps.integrations.zadara.exceptions import ZadaraError

logger = logging.getLogger(__name__)

# Short enough that an operator acting on it is not acting on yesterday, long
# enough that clicking between screens does not re-scan the cluster each time.
TTL_SECONDS = 120

# The last good picture, kept far longer than the fresh one. When Zadara is
# unreachable the panel shows this with a stale marker rather than an error page
# — an operator diagnosing an outage is exactly who needs the numbers most.
FALLBACK_TTL_SECONDS = 24 * 60 * 60

_KEY = 'platform_snapshot:v3'
_FALLBACK_KEY = 'platform_snapshot_fallback:v3'

_RUNNING = frozenset({'running', 'active'})


# Which usage fields come from which upstream list. When a list cannot be read
# its fields go to None everywhere rather than staying at zero: "this client
# stores nothing" and "we could not ask" are different answers, and only one of
# them is a reason to call the client.
FIELDS_BY_SOURCE = {
    'vms': ('vmCount', 'runningVms', 'vcpus', 'ramMB'),
    'volumes': ('volumeCount', 'storageGiB', 'storageByMedia'),
    'volumeSnapshots': ('volumeSnapshotCount', 'volumeSnapshotGiB', 'backupSnapshotCount', 'backupSnapshotGiB'),
    'vmSnapshots': ('vmSnapshotCount',),
    'publicIps': ('publicIps',),
    'vpcs': ('vpcCount',),
    'subnets': ('subnetCount',),
    'securityGroups': ('securityGroupCount',),
}

SOURCES = tuple(FIELDS_BY_SOURCE)


def _blank_usage() -> dict:
    return {
        'vmCount': 0,
        'runningVms': 0,
        'vcpus': 0,
        'ramMB': 0,
        'volumeCount': 0,
        'storageGiB': 0,
        'storageByMedia': [],
        'volumeSnapshotCount': 0,
        'volumeSnapshotGiB': 0,
        # The part of the volume snapshots that protection groups took. Always
        # counted, whatever the account's setting: the operator deciding whether
        # to bill backups needs to see how much is at stake.
        'backupSnapshotCount': 0,
        'backupSnapshotGiB': 0,
        'vmSnapshotCount': 0,
        'publicIps': 0,
        'vpcCount': 0,
        'subnetCount': 0,
        'securityGroupCount': 0,
    }


def _add_vm(usage: dict, vm: dict) -> None:
    usage['vmCount'] += 1
    usage['vcpus'] += vm.get('vcpus') or 0
    usage['ramMB'] += vm.get('ramMB') or 0
    if str(vm.get('status', '')).lower() in _RUNNING:
        usage['runningVms'] += 1


def _add_volume(usage: dict, volume: dict) -> None:
    usage['volumeCount'] += 1
    # The machine's own `diskGB` is its ROOT disk only; the volumes are what the
    # storage pool actually carries, root ones included. Measured on this
    # cluster the two differ by a quarter, so this is the number to bill from.
    usage['storageGiB'] += volume.get('sizeGiB') or 0


def _add_volume_snapshot(usage: dict, snap: dict) -> None:
    usage['volumeSnapshotCount'] += 1
    usage['volumeSnapshotGiB'] += snap.get('sizeGiB') or 0
    if backups.is_backup(snap):
        usage['backupSnapshotCount'] += 1
        usage['backupSnapshotGiB'] += snap.get('sizeGiB') or 0


_FOLD = {
    'vms': _add_vm,
    'volumes': _add_volume,
    'volumeSnapshots': _add_volume_snapshot,
    'vmSnapshots': lambda usage, row: usage.__setitem__('vmSnapshotCount', usage['vmSnapshotCount'] + 1),
    'publicIps': lambda usage, row: usage.__setitem__('publicIps', usage['publicIps'] + 1),
    'vpcs': lambda usage, row: usage.__setitem__('vpcCount', usage['vpcCount'] + 1),
    'subnets': lambda usage, row: usage.__setitem__('subnetCount', usage['subnetCount'] + 1),
    'securityGroups': lambda usage, row: usage.__setitem__('securityGroupCount', usage['securityGroupCount'] + 1),
}


def _fetch(token: str) -> tuple[dict[str, list], list[str]]:
    """Every cluster-wide list, at the same time. Names the ones that failed.

    A refused list must not take the others with it — rights and quotas differ
    per resource kind upstream, and a panel that goes blank because snapshots
    were unreadable is less useful than one that says so.
    """
    results = gather({
        'vms': lambda: zadara_resources.list_vms(token),
        'volumes': lambda: zadara_resources.list_volumes(token),
        'volumeSnapshots': lambda: zadara_resources.list_volume_snapshots(token),
        'vmSnapshots': lambda: zadara_resources.list_vm_snapshots(token),
        'publicIps': lambda: zadara_resources.list_elastic_ips(token),
        'vpcs': lambda: zadara_resources.list_vpcs(token),
        'subnets': lambda: zadara_resources.list_subnets(token),
        'securityGroups': lambda: zadara_resources.list_security_groups(token),
    })

    rows, unavailable = {}, []
    for source in SOURCES:
        outcome = results[source]
        if outcome.ok:
            rows[source] = outcome.value or []
        else:
            logger.warning('cluster snapshot: %s unavailable (%s)', source, outcome.error)
            unavailable.append(source)

    # The machine list is the one thing the panel cannot be built without: with
    # no compute there is nothing to attribute the rest to, and every screen
    # leads with it. Serve the last good snapshot instead.
    if 'vms' in unavailable:
        raise results['vms'].error

    return rows, unavailable


def _blank_account(domain: dict) -> dict:
    return {'id': domain['id'], 'name': domain['name'], 'projects': [], **_blank_usage()}


def _build() -> dict:
    started = time.monotonic()

    token = zadara_service.get_service_token()
    domains = zadara_service.list_domains()
    rows, unavailable = _fetch(token)

    accounts = {d['id']: _blank_account(d) for d in domains}
    projects: dict[str, dict] = {}

    # project id -> account id, so each row can be attributed. Directory reads
    # are cached upstream (`_svc_get_cached`), so this is cheap after the first.
    account_of_project: dict[str, str] = {}
    unreadable: list[str] = []
    for domain in domains:
        try:
            listed = zadara_service.list_domain_project_details(domain['id'])
        except ZadaraError:
            # Without its projects nothing can be attributed to this account, so
            # every figure for it is unknown — not zero. Marked as such below.
            logger.warning('could not list projects for account %s', domain['name'])
            unreadable.append(domain['name'])
            continue
        for project in listed:
            project.update(_blank_usage())
            projects[project['id']] = project
            account_of_project[project['id']] = domain['id']
        accounts[domain['id']]['projects'] = listed

    # Volumes are kept aside as well as counted: the split between SSD and HDD
    # is a per-medium tally, not a running sum, and `media_totals` already knows
    # how to make one. Two buckets, because an account id and a project id are
    # different namespaces and one dict for both would be a collision waiting.
    volumes_of_account: dict[str, list] = {}
    volumes_of_project: dict[str, list] = {}

    unattributed = {source: 0 for source in rows}
    for source, items in rows.items():
        fold = _FOLD[source]
        for row in items:
            project_id = row.get('projectId') or ''
            account_id = account_of_project.get(project_id)
            if not account_id:
                # A project created since the directory cache was filled, or one
                # in an account the service token cannot enumerate. Counted
                # separately rather than dropped, so the totals still add up.
                unattributed[source] += 1
                continue
            fold(accounts[account_id], row)
            fold(projects[project_id], row)
            if source == 'volumes':
                volumes_of_account.setdefault(account_id, []).append(row)
                volumes_of_project.setdefault(project_id, []).append(row)

    if 'volumes' in rows:
        for held, entries in ((volumes_of_account, accounts), (volumes_of_project, projects)):
            for key, entry in entries.items():
                entry['storageByMedia'] = zadara_resources.media_totals(held.get(key, []))

    totals = _totals(accounts.values(), rows)

    for entry in [*accounts.values(), *projects.values(), totals]:
        _mark_unavailable(entry, unavailable)

    # Last, so it wins over the per-source pass above: an account whose projects
    # could not be listed has no known figures at all.
    for account_entry in accounts.values():
        if account_entry['name'] in unreadable:
            _mark_unavailable(account_entry, SOURCES)

    return {
        'accounts': sorted(accounts.values(), key=lambda a: a['name'].lower()),
        'totals': totals,
        'unattributed': unattributed,
        'unavailable': unavailable,
        'unreadableAccounts': unreadable,
        'builtAt': time.time(),
        'buildSeconds': round(time.monotonic() - started, 2),
        'stale': False,
    }


def _mark_unavailable(entry: dict, unavailable: list[str]) -> None:
    """A number nobody could read is None, never zero."""
    for source in unavailable:
        for field in FIELDS_BY_SOURCE[source]:
            entry[field] = None


def _totals(accounts, rows: dict[str, list]) -> dict:
    """Cluster totals, counted from the raw lists rather than from the accounts.

    Deliberate: rows that could not be attributed to an account are still on the
    cluster and still consume it. The per-account breakdown therefore need not
    sum to these, which is what `unattributed` is for.
    """
    accounts = list(accounts)
    volumes = rows.get('volumes') or []
    snapshots = rows.get('volumeSnapshots') or []
    scheduled = [s for s in snapshots if backups.is_backup(s)]

    return {
        'accounts': len(accounts),
        'projects': sum(len(a['projects']) for a in accounts),
        'vmCount': len(rows.get('vms') or []),
        'runningVms': sum(1 for v in rows.get('vms') or [] if str(v.get('status', '')).lower() in _RUNNING),
        'vcpus': sum(v.get('vcpus') or 0 for v in rows.get('vms') or []),
        'ramMB': sum(v.get('ramMB') or 0 for v in rows.get('vms') or []),
        'volumeCount': len(volumes),
        'storageGiB': sum(v.get('sizeGiB') or 0 for v in volumes),
        'storageByMedia': zadara_resources.media_totals(volumes),
        'volumeSnapshotCount': len(snapshots),
        'volumeSnapshotGiB': sum(s.get('sizeGiB') or 0 for s in snapshots),
        'backupSnapshotCount': len(scheduled),
        'backupSnapshotGiB': sum(s.get('sizeGiB') or 0 for s in scheduled),
        'vmSnapshotCount': len(rows.get('vmSnapshots') or []),
        'publicIps': len(rows.get('publicIps') or []),
        'vpcCount': len(rows.get('vpcs') or []),
        'subnetCount': len(rows.get('subnets') or []),
        'securityGroupCount': len(rows.get('securityGroups') or []),
    }


def get(*, refresh: bool = False) -> dict:
    """The cluster snapshot. Falls back to the last good one if Zadara is down."""
    if not refresh:
        cached = cache.get(_KEY)
        if cached:
            return cached

    try:
        snapshot = _build()
    except ZadaraError:
        logger.exception('cluster snapshot failed; serving the last good one')
        fallback = cache.get(_FALLBACK_KEY)
        if fallback:
            return {**fallback, 'stale': True}
        raise

    cache.set(_KEY, snapshot, TTL_SECONDS)
    cache.set(_FALLBACK_KEY, snapshot, FALLBACK_TTL_SECONDS)

    return snapshot


def account(account_id: str) -> dict | None:
    return next((a for a in get()['accounts'] if a['id'] == account_id), None)
