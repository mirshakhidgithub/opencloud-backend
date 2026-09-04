"""
One picture of the whole cluster, assembled once and shared.

Every screen in the admin panel wants a slightly different cut of the same
facts: the account registry wants counts per account, the capacity page wants
totals, the account card wants one account's detail. Asking Zadara separately
for each would fan a single page open into dozens of upstream calls across 21
accounts, which is exactly what the cabinet was careful not to do.

So it is built once — the VM list is a single service-token call that already
spans the cluster — and cached briefly. Screens read the snapshot; nothing here
is authoritative, and `stale` says so when Zadara could not be reached and the
last good picture is being served instead.
"""

import logging
import time

from django.core.cache import cache

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

_KEY = 'platform_snapshot:v1'
_FALLBACK_KEY = 'platform_snapshot_fallback:v1'


def _blank_account(domain: dict) -> dict:
    return {
        'id': domain['id'],
        'name': domain['name'],
        'projects': [],
        'vmCount': 0,
        'runningVms': 0,
        'vcpus': 0,
        'ramMB': 0,
        'diskGB': 0,
    }


def _build() -> dict:
    started = time.monotonic()

    token = zadara_service.get_service_token()
    domains = zadara_service.list_domains()

    # One call for the entire cluster: the service token is domain-wide, so the
    # per-account split below is done here rather than by asking 21 times.
    vms = zadara_resources.list_vms(token)

    accounts = {d['id']: _blank_account(d) for d in domains}

    # project id -> account id, so each VM can be attributed. Directory reads are
    # cached upstream (`_svc_get_cached`), so this is cheap after the first pass.
    account_of_project: dict[str, str] = {}
    for domain in domains:
        try:
            projects = zadara_service.list_domain_project_details(domain['id'])
        except ZadaraError:
            logger.warning('could not list projects for account %s', domain['name'])
            continue
        accounts[domain['id']]['projects'] = projects
        for project in projects:
            account_of_project[project['id']] = domain['id']

    unattributed = 0
    for vm in vms:
        account_id = account_of_project.get(vm.get('projectId') or '')
        if not account_id:
            # A project created since the directory cache was filled, or one in
            # an account the service token cannot enumerate. Counted separately
            # rather than dropped, so the totals still add up.
            unattributed += 1
            continue
        entry = accounts[account_id]
        entry['vmCount'] += 1
        entry['vcpus'] += vm.get('vcpus') or 0
        entry['ramMB'] += vm.get('ramMB') or 0
        entry['diskGB'] += vm.get('diskGB') or 0
        if str(vm.get('status', '')).lower() in _RUNNING:
            entry['runningVms'] += 1

    return {
        'accounts': sorted(accounts.values(), key=lambda a: a['name'].lower()),
        'totals': _totals(accounts.values(), len(vms)),
        'unattributedVms': unattributed,
        'builtAt': time.time(),
        'buildSeconds': round(time.monotonic() - started, 2),
        'stale': False,
    }


_RUNNING = frozenset({'running', 'active'})


def _totals(accounts, vm_count: int) -> dict:
    accounts = list(accounts)
    return {
        'accounts': len(accounts),
        'vms': vm_count,
        'runningVms': sum(a['runningVms'] for a in accounts),
        'projects': sum(len(a['projects']) for a in accounts),
        'vcpus': sum(a['vcpus'] for a in accounts),
        'ramMB': sum(a['ramMB'] for a in accounts),
        'diskGB': sum(a['diskGB'] for a in accounts),
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
