"""
Zadara resources (VMs, ...). Uses a project-scoped token (user or service).

VM verbs, taken from the console's own bundle and confirmed against the live
API (the read-only service account is refused by policy name, which is how we
know the routes are real):

  POST {VMS}/{id}/action                 {"action": "powerup"}              vm:action
  POST {VMS}/{id}/action                 {"action": "shutdown", "force": …} vm:action
  POST {VMS}/{id}/actions/guest-reboot   (no body)                          vm:guest-reboot

`/api/v2/vms` and `/api/v2/compute/vms` are the same handler.
"""

import json
import logging
import time
from urllib.parse import urlencode, urlsplit

from django.conf import settings
from django.core.cache import cache

from apps.common.concurrency import gather

from .exceptions import ZadaraError
from .http import request

logger = logging.getLogger('zadara')


def _authed_get(path: str, token: str):
    resp = request('GET', path, token=token)
    if resp.status_code == 401:
        raise ZadaraError('invalid_credentials', 'Session token is invalid or expired', 401)
    if resp.status_code == 403:
        raise ZadaraError('forbidden', 'You are not authorized to view this resource', 403)
    if not resp.ok:
        raise ZadaraError('upstream_error', f'Zadara returned status {resp.status_code}', resp.status_code)
    try:
        return resp.json()
    except ValueError:
        return None


VMS_PATH = '/api/v2/vms'
ELASTIC_IPS_PATH = '/api/v2/vpcs/elastic-ips'

# What the API calls each verb, and the policy behind it.
VM_ACTIONS = ('start', 'stop', 'reboot')


def _authed_post(path: str, token: str, json: dict | None = None):
    resp = request('POST', path, token=token, json=json)
    if resp.status_code == 401:
        raise ZadaraError('invalid_credentials', 'Session token is invalid or expired', 401)
    if resp.status_code == 403:
        raise ZadaraError('forbidden', 'You are not authorized to perform this action', 403)
    if resp.status_code == 404:
        raise ZadaraError('not_found', 'This virtual machine no longer exists', 404)
    if resp.status_code == 409:
        raise ZadaraError('conflict', 'The machine is busy with another operation', 409)
    if not resp.ok:
        raise ZadaraError('upstream_error', f'Zadara returned status {resp.status_code}', resp.status_code)
    try:
        return resp.json()
    except ValueError:
        return None


def normalize_vm(raw: dict) -> dict:
    networks = raw.get('networks') if isinstance(raw.get('networks'), list) else []
    return {
        'id': str(raw.get('id') or ''),
        'name': str(raw.get('name') or ''),
        'status': str(raw.get('status') or 'unknown'),
        'vcpus': int(raw.get('vcpus') or 0),
        'ramMB': int(raw.get('ramMB') or 0),
        'diskGB': int(raw.get('diskGB') or 0),
        'instanceType': str(raw.get('instanceType') or ''),
        'imageId': str(raw['imageId']) if raw.get('imageId') else None,
        'createdAt': raw.get('created') or raw.get('launched_at'),
        'keyPair': raw.get('key_pair'),
        'vpcId': str(raw['vpc_id']) if raw.get('vpc_id') else None,
        'availabilityZone': raw.get('availability_zone'),
        'privateIps': [n.get('address') for n in networks if n.get('address')],
        'tags': [str(t) for t in raw.get('tags', [])] if isinstance(raw.get('tags'), list) else [],
        'projectId': str(raw['project_id']) if raw.get('project_id') else None,
    }


def list_elastic_ips(token: str) -> list[dict]:
    """Public (elastic) addresses of the token's scope.

    A VM object carries no public address at all — the console joins floating
    IPs to machines by port. This endpoint is friendlier: every record already
    names the `instance_id` it is bound to.
    """
    data = _authed_get(ELASTIC_IPS_PATH, token)
    items = data if isinstance(data, list) else (data or {}).get('elastic_ips', [])

    return [
        {
            'id': str(e.get('id') or ''),
            'publicIp': e.get('public_ip'),
            'publicDnsName': (e.get('public_dns_name') or '').rstrip('.') or None,
            'privateIp': e.get('private_ip_address'),
            'instanceId': str(e['instance_id']) if e.get('instance_id') else None,
            'networkInterfaceId': e.get('network_interface_id'),
            'projectId': str(e['project_id']) if e.get('project_id') else None,
        }
        for e in items
        if isinstance(e, dict)
    ]


def _public_ips_by_instance(token: str) -> dict[str, list[str]]:
    """instance id → its public addresses. Never fatal: a machine list is still
    worth showing when the address lookup is refused."""
    mapping: dict[str, list[str]] = {}

    try:
        for eip in list_elastic_ips(token):
            if eip['instanceId'] and eip['publicIp']:
                mapping.setdefault(eip['instanceId'], []).append(eip['publicIp'])
    except ZadaraError as err:
        logger.info('skipping public addresses: %s', err.code)

    return mapping


def list_vms(token: str, *, with_disks: bool = False) -> list[dict]:
    """List VMs visible to the token's scope (spec §3.3).

    `with_disks` joins the volume list so each machine can report the storage it
    actually uses and the medium under it. Off by default: it costs one more
    upstream request, which a caller that fetches volumes anyway should not pay.

    The machine list, the addresses and (when asked) the volumes are three
    independent reads, so they are fetched at the same time — a machine table was
    otherwise waiting out two or three round-trips in a row for data that never
    depended on each other.
    """
    sources = {
        'vms': lambda: _authed_get(VMS_PATH, token),
        'eips': lambda: _public_ips_by_instance(token),
    }
    if with_disks:
        sources['volumes'] = lambda: _safe_volumes(token)

    fetched = gather(sources)

    # Only the machine list itself is worth failing over; the other two already
    # degrade to nothing on their own.
    if not fetched['vms'].ok:
        raise fetched['vms'].error

    data = fetched['vms'].value
    items = data if isinstance(data, list) else (data or {}).get('vms', [])
    vms = [normalize_vm(v) for v in items]

    public = fetched['eips'].value or {}
    for vm in vms:
        vm['publicIps'] = public.get(vm['id'], [])

    if with_disks:
        summarise_vm_disks(vms, fetched['volumes'].value)

    return vms


def _safe_volumes(token: str) -> list[dict] | None:
    """The project's volumes, or None when they cannot be read."""
    try:
        return list_volumes(token)
    except ZadaraError as err:
        logger.info('skipping the volume join: %s', err.code)

        return None


def summarise_vm_disks(vms: list[dict], volumes: list[dict] | None) -> None:
    """Give each machine its real storage: how much, and on what.

    A machine object carries only `diskGB` — the size of its ROOT disk, verified
    against the live cluster (a VM with a 30 GB root and a 100 GiB data volume
    reports 30). What it actually uses is the volumes attached to it, and only
    those know SSD from HDD. Joined once here so a list of 155 machines does not
    fan out per row.

    `volumes=None` means the list was refused: the fields go to None/empty so a
    caller can tell "storage unknown" from "nothing attached".
    """
    grouped: dict[str, list[dict]] = {}
    for volume in volumes or []:
        if volume.get('attachedToId'):
            grouped.setdefault(volume['attachedToId'], []).append(volume)

    for vm in vms:
        attached = grouped.get(vm['id'], [])
        vm['diskGiB'] = sum(v['sizeGiB'] for v in attached) if volumes is not None else None
        vm['diskVolumes'] = len(attached)
        vm['diskMedia'] = media_totals(attached)


def _safe_elastic_ips(token: str) -> list[dict]:
    try:
        return list_elastic_ips(token)
    except ZadaraError as err:
        logger.info('skipping public addresses: %s', err.code)

        return []


def get_vm(token: str, vm_id: str) -> dict:
    """One VM, with the extra fields the list view does not carry."""
    resp = request('GET', f'{VMS_PATH}/{vm_id}', token=token)
    if resp.status_code == 404:
        raise ZadaraError('not_found', 'This virtual machine no longer exists', 404)
    if resp.status_code == 403:
        raise ZadaraError('forbidden', 'You are not authorized to view this machine', 403)
    if resp.status_code == 401:
        raise ZadaraError('invalid_credentials', 'Session token is invalid or expired', 401)
    if not resp.ok:
        raise ZadaraError('upstream_error', f'Zadara returned status {resp.status_code}', resp.status_code)

    try:
        raw = resp.json()
    except ValueError:
        raw = {}

    vm = normalize_vm(raw if isinstance(raw, dict) else {})
    # Details worth showing on a single machine but noise in a list.
    matching = [e for e in _safe_elastic_ips(token) if e['instanceId'] == vm['id']]
    vm.update(
        {
            'publicIps': [e['publicIp'] for e in matching if e['publicIp']],
            'publicDnsNames': [e['publicDnsName'] for e in matching if e['publicDnsName']],
            'securityGroups': raw.get('security_groups') or [],
            'subnetId': raw.get('subnet_id'),
            'bootVolumeId': raw.get('boot_volume_id'),
            'userData': bool(raw.get('user_data')),
            'launchedAt': raw.get('launched_at'),
            'description': raw.get('description'),
        }
    )
    return vm


def vm_action(token: str, vm_id: str, action: str, force: bool = False) -> None:
    """Start, stop or reboot a machine. Reboot is a graceful, in-guest reboot."""
    if action == 'start':
        _authed_post(f'{VMS_PATH}/{vm_id}/action', token, {'action': 'powerup'})
    elif action == 'stop':
        _authed_post(f'{VMS_PATH}/{vm_id}/action', token, {'action': 'shutdown', 'force': force})
    elif action == 'reboot':
        _authed_post(f'{VMS_PATH}/{vm_id}/actions/guest-reboot', token)
    else:
        raise ZadaraError('invalid_request', f'Unknown action: {action}', 400)


# The console client itself is Zadara-hosted static noVNC; the API call below is
# what prepares the session for the machine.
CONSOLE_CLIENT_PATH = '/vnc/vnc_auto.html'


def _absolute(url: str | None) -> str | None:
    """The session address may come back relative to the cloud's own host."""
    if not url:
        return None
    if url.startswith(('http://', 'https://')):
        return url

    return f'{settings.ZADARA_API_URL.rstrip("/")}/{url.lstrip("/")}'


def get_vm_console(token: str, vm_id: str) -> dict:
    """Open a VNC console session for one machine.

    Observed on this deployment: 200 with a JSON body that is a bare **string** —
    the address of the prepared noVNC session, which may be relative. A dict body
    or a redirect are handled too; without an address the caller has nothing to
    open (the bare client page fails with noVNC error 1006).
    """
    resp = request('GET', f'{VMS_PATH}/{vm_id}/vnc', token=token, allow_redirects=False)

    if resp.status_code == 404:
        raise ZadaraError('not_found', 'This virtual machine no longer exists', 404)
    if resp.status_code == 403:
        raise ZadaraError('console_forbidden', 'Console access is not allowed for this machine', 403)
    if resp.status_code == 401:
        raise ZadaraError('invalid_credentials', 'Session token is invalid or expired', 401)
    if not (resp.ok or resp.is_redirect):
        raise ZadaraError('upstream_error', f'Zadara returned status {resp.status_code}', resp.status_code)

    content_type = resp.headers.get('Content-Type', '')
    body = None
    if 'json' in content_type:
        try:
            body = resp.json()
        except ValueError:
            body = None

    url = resp.headers.get('Location')
    if not url and isinstance(body, str):
        url = body.strip() or None
    if not url and isinstance(body, dict):
        for key in ('url', 'console_url', 'link', 'href'):
            if body.get(key):
                url = str(body[key])
                break
    if not url and isinstance(body, dict) and isinstance(body.get('console'), dict):
        url = body['console'].get('url')

    url = _absolute(url)

    logger.info(
        'vnc session for %s: status=%s body=%s url=%s',
        vm_id,
        resp.status_code,
        type(body).__name__,
        # The address carries the session credential — log only where it points.
        urlsplit(url).path if url else None,
    )

    if not url:
        raise ZadaraError('console_unavailable', 'The cloud did not return a console address', 502)

    return {'url': url, 'clientPath': CONSOLE_CLIENT_PATH, 'status': resp.status_code}


VOLUMES_PATH = '/api/v3/volumes'
VOLUME_SNAPSHOTS_PATH = '/api/v4/snapshots'
VM_SNAPSHOTS_PATH = '/api/v2/compute/vm-snapshots'
VPCS_PATH = '/api/v2/vpcs'
SUBNETS_PATH = '/api/v2/vpcs/networks'
SECURITY_GROUPS_PATH = '/api/v2/vpcs/security-groups'
INTERNET_GATEWAYS_PATH = '/api/v2/vpcs/internet-gateways'
NAT_GATEWAYS_PATH = '/api/v2/vpcs/nat-gateways'

# Rules are Neutron objects, not part of the group document: they carry their own
# ids, and Neutron has no PUT for them — a rule is created or deleted, never
# edited. The UI has to say so rather than pretend otherwise.
SG_RULES_PATH = '/api/openstack/networking/v2.0/security-group-rules'
ALARMS_PATH = '/api/v2/alarm/alarms'


def _items(data, key: str) -> list:
    if isinstance(data, list):
        return data
    return (data or {}).get(key, []) or []


# ---------------------------------------------------------------------------
# Volume types — where SSD vs HDD actually comes from
#
# A volume names only a type id (in the misleadingly named `storage_pool`), and
# the type list is the only place the medium is written down. The cloud
# publishes no medium field and no storage-class route at all (every
# /storage-classes path answers 404), so it is read off the type's own name and
# description — which on this cluster always say it: `uz_serv_01_EBS_SSD_1`,
# `zvthdd3`, 'Zadara Hard Drive Device 1'. A type whose text says nothing
# inherits from a sibling in the same storage class; the four classes here are
# each wholly one medium. When neither settles it the label stays empty rather
# than guessing — an unlabelled disk is honest, a mislabelled one is not.
#
# The list is cluster-wide (ten types, identical for every account) and changes
# only when an operator adds one, so it is cached rather than fetched per page.
# ---------------------------------------------------------------------------

VOLUME_TYPES_PATH = '/api/v4/volumes/volume-types'

_SSD_HINTS = ('ssd', 'flash', 'nvme', 'general purpose', 'general-purpose')
_HDD_HINTS = ('hdd', 'hard drive', 'hard-drive', 'spinning', 'sata')

_VOLUME_TYPES_CACHE_KEY = 'zadara:volume_types'
VOLUME_TYPES_CACHE_TTL = 15 * 60


def _media_from_text(text: str) -> str:
    lowered = text.lower()
    ssd = any(hint in lowered for hint in _SSD_HINTS)
    hdd = any(hint in lowered for hint in _HDD_HINTS)

    if ssd and not hdd:
        return 'SSD'
    if hdd and not ssd:
        return 'HDD'

    return ''


def _classify_volume_types(raw: list) -> dict[str, dict]:
    types: dict[str, dict] = {}

    for entry in raw:
        if not isinstance(entry, dict) or not entry.get('id'):
            continue

        name = entry.get('name') or ''
        description = entry.get('description') or ''
        types[str(entry['id'])] = {
            'id': str(entry['id']),
            'name': name,
            'description': description,
            # AWS-style shorthand the cloud shows next to the type (gp3, sc1, …).
            'alias': entry.get('alias') or '',
            'storageClassId': str(entry.get('storage_class_id') or ''),
            'available': bool(entry.get('is_available')),
            'media': _media_from_text(f'{name} {description}'),
        }

    by_class: dict[str, set] = {}
    for entry in types.values():
        if entry['media'] and entry['storageClassId']:
            by_class.setdefault(entry['storageClassId'], set()).add(entry['media'])

    for entry in types.values():
        if entry['media']:
            continue

        siblings = by_class.get(entry['storageClassId']) or set()
        if len(siblings) == 1:
            entry['media'] = next(iter(siblings))

    return types


def volume_type_index(token: str) -> dict[str, dict]:
    """Volume types by id, each with the medium it sits on."""
    cached = cache.get(_VOLUME_TYPES_CACHE_KEY)
    if cached is not None:
        return cached

    types = _classify_volume_types(_authed_get(VOLUME_TYPES_PATH, token) or [])
    cache.set(_VOLUME_TYPES_CACHE_KEY, types, VOLUME_TYPES_CACHE_TTL)

    return types


def safe_volume_type_index(token: str) -> dict[str, dict]:
    """The type index, or nothing — never a reason to fail a storage page."""
    try:
        return volume_type_index(token)
    except ZadaraError:
        logger.warning('volume types unavailable; storage will be shown unlabelled')

        return {}


def list_volume_types(token: str) -> list[dict]:
    """Every volume type, for pages that offer a choice of medium."""
    types = volume_type_index(token).values()

    return sorted(types, key=lambda t: (t['media'], t['name'].lower()))


def label_volume_media(volumes: list[dict], types: dict[str, dict]) -> None:
    """Name the type and the medium on already-normalised volumes, in place."""
    for volume in volumes:
        entry = types.get(volume.get('volumeTypeId') or '')
        volume['volumeType'] = entry['name'] if entry else ''
        volume['media'] = entry['media'] if entry else ''


def media_totals(volumes: list[dict]) -> list[dict]:
    """Capacity per medium, biggest first — 'Unknown' last if anything is unlabelled."""
    buckets: dict[str, dict] = {}

    for volume in volumes:
        bucket = buckets.setdefault(volume.get('media') or '', {'volumes': 0, 'totalGiB': 0})
        bucket['volumes'] += 1
        bucket['totalGiB'] += volume.get('sizeGiB') or 0

    return sorted(
        ({'media': media, **totals} for media, totals in buckets.items()),
        key=lambda row: (row['media'] == '', -row['totalGiB']),
    )


def _attached_instance(raw: dict) -> str | None:
    """The machine a volume serves, if any.

    The cloud nests it as attachment.hosts[].instance_id rather than putting an
    id on the volume itself.
    """
    attachment = raw.get('attachment')
    if not isinstance(attachment, dict):
        return None

    for host in attachment.get('hosts') or []:
        if isinstance(host, dict) and host.get('instance_id'):
            return str(host['instance_id'])

    return None


def normalize_volume(raw: dict) -> dict:
    permissions = raw.get('permissions') if isinstance(raw.get('permissions'), dict) else {}

    return {
        'id': str(raw.get('id') or ''),
        'name': raw.get('name') or '',
        'sizeGiB': int(raw.get('size_gib') or 0),
        'state': raw.get('state') or 'unknown',
        'health': raw.get('health') or 'unknown',
        'attachmentStatus': raw.get('attachment_status') or 'available',
        'attachedToId': _attached_instance(raw),
        'guestDevice': raw.get('guest_device_name'),
        'accessMode': raw.get('access_mode'),
        # Misleadingly named upstream: `storage_pool` holds a VOLUME TYPE id
        # (it matches /api/v4/volumes/volume-types), which is what says whether
        # the volume sits on SSD or HDD. Labelled by list_volumes.
        'volumeTypeId': str(raw['storage_pool']) if raw.get('storage_pool') else None,
        'volumeType': '',
        'media': '',
        'createdAt': raw.get('created_at'),
        'projectId': str(raw['project_id']) if raw.get('project_id') else None,
        # What the caller may do with it, as judged by the cloud, not by us.
        'canUpdate': bool(permissions.get('update')),
        'canDelete': bool(permissions.get('delete')),
    }


def list_volumes(token: str, *, label_media: bool = True) -> list[dict]:
    """Block volumes of the token's scope, each labelled SSD or HDD.

    The medium comes from a second, cached request: a volume names only its type
    id, and every page that lists storage wants to say what it sits on. If that
    request is refused the volumes still come back, just unlabelled.
    """
    data = _authed_get(VOLUMES_PATH, token)
    volumes = [normalize_volume(v) for v in _items(data, 'volumes') if isinstance(v, dict)]

    if label_media:
        label_volume_media(volumes, safe_volume_type_index(token))

    return volumes


def list_vpcs(token: str) -> list[dict]:
    data = _authed_get(VPCS_PATH, token)

    return [
        {
            'id': str(v.get('id') or ''),
            'name': v.get('name') or '',
            'cidrBlock': v.get('cidr_block'),
            'state': v.get('state') or 'unknown',
            'isDefault': bool(v.get('is_default')),
            'projectId': str(v['project_id']) if v.get('project_id') else None,
        }
        for v in _items(data, 'vpcs')
        if isinstance(v, dict)
    ]


def list_alarms(token: str) -> list[dict]:
    """Alarms of the token's scope. `state` is 'closed' once it is over."""
    data = _authed_get(ALARMS_PATH, token)

    return [
        {
            'id': str(a.get('id') or ''),
            'type': a.get('type_name') or '',
            'state': (a.get('state') or '').lower(),
            'entityType': a.get('entity_type') or '',
            'entityId': a.get('entity_id') or '',
            'createdAt': a.get('created_at'),
            'projectId': str(a['project_id']) if a.get('project_id') else None,
        }
        for a in _items(data, 'alarms')
        if isinstance(a, dict)
    ]


METRICS_PATH = '/api/v2/metrics/queries'

# Per-VM metrics this deployment publishes. There is **no disk-utilisation
# metric at any level a tenant can see**: the per-pool Ceph counters exist in the
# console's code but this cluster answers "metric doesn't exist" for them, and
# disk space is only published per storage node. Volumes are therefore described
# by size and attachment from /api/v3/volumes, never by a usage curve.
VM_METRICS = {
    'cpuPercent': 'cpu__used__of__vm__in__percent',
    'memoryUsedMiB': 'memory__used_by_guest_os__of__vm__in__MiB',
    'memorySizeMiB': 'memory__size__of__vm__in__MiB',
    'networkRxKbps': 'network__rx_kbps__of__vm__in__kbps',
    'networkTxKbps': 'network__tx_kbps__of__vm__in__kbps',
}

# period -> (seconds back, bucket size, bucket unit). Buckets are chosen to keep
# every window around 100-200 points: enough shape, small enough payload.
METRIC_PERIODS = {
    '1h': (3600, 3, 'minutes'),
    '6h': (6 * 3600, 10, 'minutes'),
    '24h': (24 * 3600, 30, 'minutes'),
    '7d': (7 * 86400, 3, 'hours'),
    '30d': (30 * 86400, 12, 'hours'),
}


# Whole-project counters. The cloud aggregates these itself, which is both
# cheaper and more truthful than summing per-machine series on our side.
PROJECT_METRICS = {
    'cpuMHz': 'cpu__used__of__tenant__in__MHz',
    'memoryMiB': 'memory__used__of__tenant__in__MiB',
    'networkRxKbps': 'network__rx_kbps__of__tenant__in__kbps',
    'networkTxKbps': 'network__tx_kbps__of__tenant__in__kbps',
    'networkRxErrors': 'network__rx_errors__of__tenant__in__packets_per_sec',
    'networkTxErrors': 'network__tx_errors__of__tenant__in__packets_per_sec',
}


def _query_history(token: str, metrics: dict[str, str], entity_id: str, period: str) -> dict:
    """Run one history query per metric for a single entity and normalise it."""
    if period not in METRIC_PERIODS:
        raise ZadaraError('invalid_request', f'Unknown period: {period}', 400)

    span, interval, unit = METRIC_PERIODS[period]
    end = int(time.time())
    start = end - span

    queries = {
        key: [
            'query_history_group_by_time',
            {
                'metric_name': name,
                'entity_ids': [entity_id],
                'start_timestamp': start,
                'end_timestamp': end,
                'statistic': 'max',
                'interval': interval,
                'time_type': unit,
                'fill_type': 'null',
            },
        ]
        for key, name in metrics.items()
    }

    data = _authed_get(f'{METRICS_PATH}?queries={json.dumps(queries)}', token) or {}

    series: dict[str, list] = {}
    for key in metrics:
        entries = data.get(key)
        # A metric the cluster does not collect comes back as an error string
        # rather than a list — treat it as "no data", not as a failure.
        points = entries[0].get('points') if isinstance(entries, list) and entries and isinstance(entries[0], dict) else None
        series[key] = [[int(ts), round(float(value), 3)] for ts, value in (points or []) if value is not None]

    return {
        'period': period,
        'from': start * 1000,
        'to': end * 1000,
        'intervalMinutes': interval * (60 if unit == 'hours' else 1),
        'series': series,
    }


def get_project_metrics(token: str, project_id: str, period: str = '24h') -> dict:
    """Utilisation of a whole project, as the cloud itself aggregates it."""
    return _query_history(token, PROJECT_METRICS, project_id, period)


def get_vm_metrics(token: str, vm_id: str, period: str = '24h') -> dict:
    """Time series for one machine, ready to plot.

    Returns `{key: [[unix_ms, value], ...]}`; a metric the cloud has nothing for
    comes back as an empty list rather than being missing, so the caller can
    tell "no data" from "not asked".
    """
    return _query_history(token, VM_METRICS, vm_id, period)


def list_volume_snapshots(token: str) -> list[dict]:
    """Point-in-time copies of single volumes."""
    data = _authed_get(VOLUME_SNAPSHOTS_PATH, token)

    return [
        {
            'id': str(s.get('id') or ''),
            'name': s.get('name') or '',
            'description': s.get('description') or None,
            'sizeGiB': int(s.get('size_gib') or 0),
            'state': s.get('state') or 'unknown',
            'health': s.get('health') or 'unknown',
            'sourceVolumeId': str(s['source_volume_id']) if s.get('source_volume_id') else None,
            'protectionGroupId': s.get('protection_group_id'),
            'createdAt': s.get('created_at'),
            'projectId': str(s['project_id']) if s.get('project_id') else None,
        }
        for s in _items(data, 'snapshots')
        if isinstance(s, dict)
    ]


def list_vm_snapshots(token: str) -> list[dict]:
    """Copies of a whole machine — every volume of it at one moment."""
    data = _authed_get(VM_SNAPSHOTS_PATH, token)

    return [
        {
            'id': str(s.get('id') or ''),
            'name': s.get('name') or '',
            'description': s.get('description') or None,
            'status': s.get('status') or 'unknown',
            'progress': s.get('progress'),
            'volumes': int(s.get('number_of_volumes') or 0),
            'sourceVmId': str(s['source_vm_id']) if s.get('source_vm_id') else None,
            'sourceVmName': s.get('source_vm_name') or '',
            'incremental': bool(s.get('enable_incremental_backup')),
            'protectionGroup': s.get('protection_group_name'),
            'createdAt': s.get('created_at'),
            'projectId': str(s['project_id']) if s.get('project_id') else None,
        }
        for s in _items(data, 'vm_snapshots')
        if isinstance(s, dict)
    ]


def list_subnets(token: str) -> list[dict]:
    """Networks inside VPCs, with how much address space is left in each."""
    data = _authed_get(SUBNETS_PATH, token)

    return [
        {
            'id': str(n.get('id') or ''),
            'name': n.get('name') or '',
            'cidrBlock': n.get('cidr_block'),
            'vpcId': str(n['vpc_id']) if n.get('vpc_id') else None,
            'state': n.get('state') or 'unknown',
            'isDefault': bool(n.get('is_default')),
            'isDirect': bool(n.get('is_direct_network')),
            'networkType': n.get('network_type'),
            'mtu': n.get('mtu'),
            'totalIps': int(n.get('total_ip_address_count') or 0),
            'availableIps': int(n.get('available_ip_address_count') or 0),
            'projectId': str(n['project_id']) if n.get('project_id') else None,
        }
        for n in _items(data, 'networks')
        if isinstance(n, dict)
    ]


def _describe_rule(rule: dict) -> str:
    """One firewall rule as a person would read it: 'tcp 22 from 0.0.0.0/0'."""
    protocol = str(rule.get('ip_protocol') or 'any')
    if protocol == '-1':
        protocol = 'any'

    start, end = rule.get('from_port'), rule.get('to_port')
    if start in (None, 0) and end in (None, 0, 65535):
        ports = 'all ports'
    elif start == end or end is None:
        ports = str(start)
    else:
        ports = f'{start}-{end}'

    sources = [r.get('cidr_ip') for r in (rule.get('ip_ranges') or []) if isinstance(r, dict) and r.get('cidr_ip')]
    sources += ['group' for g in (rule.get('groups') or []) if isinstance(g, dict)]

    return f'{protocol} {ports} · {", ".join(sources) if sources else "no source"}'


def list_security_groups(token: str) -> list[dict]:
    data = _authed_get(SECURITY_GROUPS_PATH, token)

    return [
        {
            'id': str(g.get('id') or ''),
            'name': g.get('name') or '',
            'description': g.get('description') or None,
            'vpcId': str(g['vpc_id']) if g.get('vpc_id') else None,
            'ingress': [_describe_rule(r) for r in (g.get('ip_permissions_ingress') or []) if isinstance(r, dict)],
            'egress': [_describe_rule(r) for r in (g.get('ip_permissions_egress') or []) if isinstance(r, dict)],
            'projectId': str(g['project_id']) if g.get('project_id') else None,
        }
        for g in _items(data, 'security_groups')
        if isinstance(g, dict)
    ]


def list_internet_gateways(token: str) -> list[dict]:
    data = _authed_get(INTERNET_GATEWAYS_PATH, token)
    gateways = []

    for g in _items(data, 'internet_gateways'):
        if not isinstance(g, dict):
            continue
        attachments = [a for a in (g.get('attachment_set') or []) if isinstance(a, dict)]
        gateways.append(
            {
                'id': str(g.get('id') or ''),
                'name': g.get('name') or '',
                # A gateway is only useful once attached to a VPC.
                'vpcIds': [str(a['vpc_id']) for a in attachments if a.get('vpc_id')],
                'state': attachments[0].get('state') if attachments else 'detached',
                'projectId': str(g['project_id']) if g.get('project_id') else None,
            }
        )

    return gateways


def list_nat_gateways(token: str) -> list[dict]:
    data = _authed_get(NAT_GATEWAYS_PATH, token)

    return [
        {
            'id': str(g.get('id') or ''),
            'name': g.get('name') or '',
            'publicIp': g.get('public_ip'),
            'privateIp': g.get('private_ip'),
            'state': g.get('state') or 'unknown',
            'vpcId': str(g['vpc_id']) if g.get('vpc_id') else None,
            'failure': g.get('failure_message'),
            'createdAt': g.get('created_at'),
            'projectId': str(g['project_id']) if g.get('project_id') else None,
        }
        for g in _items(data, 'nat_gateways')
        if isinstance(g, dict) and not g.get('deleted_at')
    ]


def _rule_label(rule: dict) -> str:
    """A Neutron rule as a person reads it: 'tcp 22 · 0.0.0.0/0'."""
    protocol = rule.get('protocol') or 'any'
    start, end = rule.get('port_range_min'), rule.get('port_range_max')

    if start is None and end is None:
        ports = 'all ports'
    elif start == end:
        ports = str(start)
    else:
        ports = f'{start}-{end}'

    if rule.get('remote_ip_prefix'):
        source = rule['remote_ip_prefix']
    elif rule.get('remote_group_id'):
        source = 'another group'
    else:
        source = 'anywhere'

    return f'{protocol} {ports} · {source}'


def normalize_security_group_rule(raw: dict) -> dict:
    return {
        'id': str(raw.get('id') or ''),
        'groupId': str(raw.get('security_group_id') or ''),
        'direction': raw.get('direction') or 'ingress',
        'ethertype': raw.get('ethertype') or 'IPv4',
        'protocol': raw.get('protocol'),
        'portFrom': raw.get('port_range_min'),
        'portTo': raw.get('port_range_max'),
        'cidr': raw.get('remote_ip_prefix'),
        'remoteGroupId': raw.get('remote_group_id'),
        'description': raw.get('description') or '',
        'label': _rule_label(raw),
    }


def list_security_group_rules(token: str) -> list[dict]:
    """Every firewall rule the token can see, with the id needed to remove one."""
    data = _authed_get(SG_RULES_PATH, token)

    return [normalize_security_group_rule(r) for r in _items(data, 'security_group_rules') if isinstance(r, dict)]


def create_security_group_rule(
    token: str,
    group_id: str,
    direction: str,
    protocol: str | None = None,
    port_from: int | None = None,
    port_to: int | None = None,
    cidr: str | None = None,
    remote_group_id: str | None = None,
    description: str = '',
    ethertype: str = 'IPv4',
) -> dict:
    rule = {
        'security_group_id': group_id,
        'direction': direction,
        'ethertype': ethertype,
        'description': description,
    }
    # Neutron wants the keys absent, not null, when they do not apply.
    if protocol:
        rule['protocol'] = protocol
    if port_from is not None:
        rule['port_range_min'] = port_from
        rule['port_range_max'] = port_to if port_to is not None else port_from
    if cidr:
        rule['remote_ip_prefix'] = cidr
    elif remote_group_id:
        rule['remote_group_id'] = remote_group_id

    resp = request('POST', SG_RULES_PATH, token=token, json={'security_group_rule': rule})

    if resp.ok:
        pass
    else:
        # Neutron explains itself in the body; without it a bare status tells
        # nobody anything (a 404 here can mean the group, the project scope or
        # the route).
        message = _neutron_message(resp)
        logger.info('security group rule refused: %s %s | sent %s', resp.status_code, message[:200], sorted(rule))

        if resp.status_code in (401, 403):
            raise ZadaraError('forbidden', message or 'You are not allowed to change this security group', resp.status_code)
        if resp.status_code == 409:
            raise ZadaraError('conflict', message or 'That rule already exists in this group', 409)
        if resp.status_code == 400:
            raise ZadaraError('invalid_request', message or 'The cloud rejected this rule', 400)
        if resp.status_code == 404:
            # Two very different 404s arrive here. Neutron answers JSON when it
            # really cannot find the group; the gateway answers an HTML page when
            # it refuses the write outright — observed for tenant-admin tokens,
            # which may read this API but not write to it.
            if not message:
                raise ZadaraError(
                    'write_not_allowed',
                    'This cloud does not accept firewall changes from your account through this path.',
                    404,
                )
            raise ZadaraError('not_found', message, 404)
        raise ZadaraError('upstream_error', message or f'Zadara returned status {resp.status_code}', resp.status_code)

    try:
        created = (resp.json() or {}).get('security_group_rule') or {}
    except ValueError:
        created = {}

    return normalize_security_group_rule(created)


def delete_security_group_rule(token: str, rule_id: str) -> None:
    resp = request('DELETE', f'{SG_RULES_PATH}/{rule_id}', token=token)

    if resp.status_code in (401, 403):
        raise ZadaraError('forbidden', 'You are not allowed to change this security group', resp.status_code)
    if resp.status_code == 404:
        raise ZadaraError('not_found', 'That rule no longer exists', 404)
    if not resp.ok:
        raise ZadaraError('upstream_error', f'Zadara returned status {resp.status_code}', resp.status_code)


def _neutron_message(resp) -> str:
    try:
        body = resp.json() or {}
    except ValueError:
        return ''

    error = body.get('NeutronError')

    return str(error.get('message') or '') if isinstance(error, dict) else ''


# ---------------------------------------------------------------------------
# Quotas
#
# Two routes, the same 35 rows, different meaning:
#   /api/v2/quotas/limits/domains/{id}   ceilings for the whole ACCOUNT, and
#                                        `allocated` summed over its projects
#   /api/v2/quotas/limits/projects/{id}  what one PROJECT consumes
#
# On this cluster only the account carries real ceilings (cores, instances,
# RAM); every project row answers `total: null`. So `total is None` means "no
# limit configured", never "zero allowed" — the UI must not draw a full bar for
# it. There is no `/api/v2/quotas` collection route (404); the id is required.
# ---------------------------------------------------------------------------

QUOTA_DOMAIN_PATH = '/api/v2/quotas/limits/domains/{}'
QUOTA_PROJECT_PATH = '/api/v2/quotas/limits/projects/{}'


def _quota_volume_type(raw: dict) -> str | None:
    """The volume type a storage quota row is about, if it is about one.

    Storage rows come one per volume type (`volumes_<id>`, `volume_megabytes_<id>`)
    and name it under `dependencies`, which is how a row can be told SSD from HDD.
    """
    for dependency in raw.get('dependencies') or []:
        if isinstance(dependency, dict) and dependency.get('type') == 'volume_type' and dependency.get('id'):
            return str(dependency['id'])

    return None


def normalize_quota(raw: dict) -> dict:
    total = raw.get('total')
    allocated = raw.get('allocated') or 0
    limited = isinstance(total, (int, float)) and not isinstance(total, bool)

    return {
        'name': str(raw.get('name') or ''),
        'displayName': raw.get('display_name') or str(raw.get('name') or ''),
        # `domain` here is the service family (compute/storage/services), not a
        # Keystone domain — renamed so it cannot be mistaken for the account.
        'group': raw.get('domain') or '',
        'category': raw.get('category') or '',
        'units': raw.get('units') or '',
        'kind': raw.get('type') or 'count',
        'allocated': allocated,
        'limit': total if limited else None,
        'unlimited': not limited,
        'usedPercent': round(allocated / total * 100, 1) if limited and total else None,
        'volumeTypeId': _quota_volume_type(raw),
        'volumeType': '',
        'media': '',
    }


def label_quota_media(rows: list[dict], types: dict[str, dict]) -> None:
    """Say SSD or HDD on the storage rows, in place."""
    for row in rows:
        entry = types.get(row.get('volumeTypeId') or '')
        row['volumeType'] = entry['name'] if entry else ''
        row['media'] = entry['media'] if entry else ''


def _list_quotas(path: str, token: str) -> list[dict]:
    quotas = [normalize_quota(q) for q in (_authed_get(path, token) or []) if isinstance(q, dict)]
    label_quota_media(quotas, safe_volume_type_index(token))

    # Storage rows are per volume type, so the medium sorts before the name:
    # every SSD row together, every HDD row together, not interleaved by name.
    quotas.sort(key=lambda q: (q['group'], q['category'], q['media'], q['displayName'].lower()))

    return quotas


def list_domain_quotas(token: str, domain_id: str) -> list[dict]:
    """Account-wide allocation and the ceilings it is measured against."""
    return _list_quotas(QUOTA_DOMAIN_PATH.format(domain_id), token)


def list_project_quotas(token: str, project_id: str) -> list[dict]:
    """What a single project consumes of the account's allowance."""
    return _list_quotas(QUOTA_PROJECT_PATH.format(project_id), token)


# ---------------------------------------------------------------------------
# Events — the cloud's own log, which is not our audit trail
#
# GET /api/v2/events/query accepts exactly (the API names them itself in a 400):
#   start_timestamp, end_timestamp   milliseconds, both required
#   limit, offset                    paging; there is no total count
#   project_id, entity_id, entity_type, event_type, user_id, hostname, request_id
#   severity                         INFO | WARNING | ERROR
#
# `type_id` is NOT a parameter — the filter is `event_type`. Unfiltered, a
# service token sees the whole cluster (3346 events in 7 days across 21
# accounts), so callers here must always pin project_id.
# ---------------------------------------------------------------------------

EVENTS_PATH = '/api/v2/events/query'

EVENT_PERIODS = {
    '1h': 3600,
    '24h': 24 * 3600,
    '7d': 7 * 86400,
    '30d': 30 * 86400,
}

EVENT_SEVERITIES = ('INFO', 'WARNING', 'ERROR')

# Enough to count and page a project's activity honestly; a busier window is
# reported as truncated rather than silently cut.
EVENT_FETCH_CAP = 1000


def normalize_event(raw: dict) -> dict:
    return {
        'id': str(raw.get('id') or ''),
        'timestamp': raw.get('timestamp'),
        'severity': (raw.get('severity') or 'INFO').upper(),
        'typeId': raw.get('type_id') or '',
        'typeName': raw.get('type_name') or '',
        'description': raw.get('description') or '',
        'entityType': raw.get('entity_type') or '',
        'entityName': raw.get('entity_name') or '',
        'entityId': str(raw.get('entity_id') or ''),
        'projectId': str(raw['project_id']) if raw.get('project_id') else None,
        'userId': str(raw['user_id']) if raw.get('user_id') else None,
        'originIp': raw.get('origin_ip') or '',
        'hostname': raw.get('hostname') or '',
        'requestId': str(raw.get('request_id') or ''),
        'account': raw.get('domain') or raw.get('account_name') or '',
    }


def list_events(
    token: str,
    *,
    project_id: str,
    period: str = '24h',
    severity: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    event_type: str | None = None,
    limit: int = EVENT_FETCH_CAP,
) -> dict:
    """One window of a project's events, newest first.

    Returns the whole window (up to `limit`) rather than a page: the API reports
    no total, and a page without counts cannot say how much happened. The caller
    slices it for display.
    """
    if period not in EVENT_PERIODS:
        raise ZadaraError('invalid_request', f'Unknown period: {period}', 400)
    if severity and severity.upper() not in EVENT_SEVERITIES:
        raise ZadaraError('invalid_request', f'Unknown severity: {severity}', 400)

    end = int(time.time() * 1000)
    start = end - EVENT_PERIODS[period] * 1000

    params = {
        'start_timestamp': start,
        'end_timestamp': end,
        'limit': limit,
        'project_id': project_id,
    }
    optional = {
        'severity': severity.upper() if severity else None,
        'entity_type': entity_type,
        'entity_id': entity_id,
        'event_type': event_type,
    }
    params.update({k: v for k, v in optional.items() if v})

    data = _authed_get(f'{EVENTS_PATH}?{urlencode(params)}', token) or []
    events = [normalize_event(e) for e in data if isinstance(e, dict)]
    events.sort(key=lambda e: e['timestamp'] or 0, reverse=True)

    if len(events) >= limit:
        logger.warning('events window truncated at %s for project %s (%s)', limit, project_id, period)

    return {
        'events': events,
        'from': start,
        'to': end,
        'period': period,
        'truncated': len(events) >= limit,
    }
