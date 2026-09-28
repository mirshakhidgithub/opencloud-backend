"""
What each client consumes, and how that accounting can go quietly wrong.

The platform snapshot attributes every resource on the cluster to an account by
the project it sits in. Three ways that is easy to break, all silent:

  * a row lands on the wrong account, or on every account;
  * a list nobody could read is reported as zero, so a client with forty
    terabytes of snapshots looks like a client with none;
  * storage is counted from the machines' root disks instead of the volumes,
    which on this cluster is a quarter short.
"""

import pytest

from apps.platform_admin import snapshot
from apps.integrations.zadara.exceptions import ZadaraError


DOMAINS = [{'id': 'dom-a', 'name': 'Acme'}, {'id': 'dom-b', 'name': 'Beta'}]

PROJECTS = {
    'dom-a': [{'id': 'p-a1', 'name': 'a-main', 'description': None, 'enabled': True, 'isVpc': True},
              {'id': 'p-a2', 'name': 'a-lab', 'description': None, 'enabled': True, 'isVpc': False}],
    'dom-b': [{'id': 'p-b1', 'name': 'b-main', 'description': None, 'enabled': True, 'isVpc': True}],
}

VMS = [
    {'id': 'vm-1', 'projectId': 'p-a1', 'status': 'running', 'vcpus': 4, 'ramMB': 8192, 'diskGB': 50},
    {'id': 'vm-2', 'projectId': 'p-a2', 'status': 'stopped', 'vcpus': 2, 'ramMB': 4096, 'diskGB': 50},
    {'id': 'vm-3', 'projectId': 'p-b1', 'status': 'active', 'vcpus': 8, 'ramMB': 16384, 'diskGB': 100},
    # A project the directory does not know yet.
    {'id': 'vm-4', 'projectId': 'p-ghost', 'status': 'running', 'vcpus': 1, 'ramMB': 1024, 'diskGB': 10},
]

VOLUMES = [
    {'id': 'v-1', 'projectId': 'p-a1', 'sizeGiB': 50, 'media': 'SSD'},
    {'id': 'v-2', 'projectId': 'p-a1', 'sizeGiB': 500, 'media': 'HDD'},
    {'id': 'v-3', 'projectId': 'p-a2', 'sizeGiB': 50, 'media': 'SSD'},
    {'id': 'v-4', 'projectId': 'p-b1', 'sizeGiB': 100, 'media': 'SSD'},
]

VOLUME_SNAPSHOTS = [
    # Taken by a protection group — a scheduled backup.
    {'id': 's-1', 'projectId': 'p-a1', 'sizeGiB': 200, 'protectionGroupId': 'pg-1'},
    {'id': 's-2', 'projectId': 'p-b1', 'sizeGiB': 30},
]

LISTS = {
    'list_vms': VMS,
    'list_volumes': VOLUMES,
    'list_volume_snapshots': VOLUME_SNAPSHOTS,
    'list_vm_snapshots': [{'id': 'vs-1', 'projectId': 'p-a1'}],
    'list_elastic_ips': [{'id': 'e-1', 'projectId': 'p-a1'}, {'id': 'e-2', 'projectId': 'p-b1'}],
    'list_vpcs': [{'id': 'c-1', 'projectId': 'p-a1'}],
    'list_subnets': [{'id': 'n-1', 'projectId': 'p-a1'}, {'id': 'n-2', 'projectId': 'p-a2'}],
    'list_security_groups': [{'id': 'g-1', 'projectId': 'p-b1'}],
}


@pytest.fixture
def cluster(monkeypatch):
    """The whole cluster, patched at the Zadara client boundary.

    `broken` names lists that refuse to answer, which is the case worth testing
    separately: those must not read as zero.
    """

    def _cluster(broken=()):
        monkeypatch.setattr(snapshot.zadara_service, 'get_service_token', lambda: 'svc-token')
        monkeypatch.setattr(snapshot.zadara_service, 'list_domains', lambda: DOMAINS)
        monkeypatch.setattr(
            snapshot.zadara_service,
            'list_domain_project_details',
            lambda domain_id: [dict(p) for p in PROJECTS[domain_id]],
        )

        for name, rows in LISTS.items():
            if name in broken:
                def _refuse(*args, _name=name, **kwargs):
                    raise ZadaraError('forbidden', f'{_name} is not readable', 403)

                monkeypatch.setattr(snapshot.zadara_resources, name, _refuse)
            else:
                monkeypatch.setattr(snapshot.zadara_resources, name, lambda token, _rows=rows: [dict(r) for r in _rows])

        return snapshot.get(refresh=True)

    return _cluster


def account_of(snap, name):
    return next(a for a in snap['accounts'] if a['name'] == name)


def project_of(snap, account, project):
    return next(p for p in account_of(snap, account)['projects'] if p['name'] == project)


def test_every_kind_of_resource_is_counted_per_account(cluster):
    """Not just compute: the whole footprint, or a storage-heavy tenant reads as small."""
    acme = account_of(cluster(), 'Acme')

    assert (acme['vmCount'], acme['runningVms'], acme['vcpus'], acme['ramMB']) == (2, 1, 6, 12288)
    assert (acme['volumeCount'], acme['storageGiB']) == (3, 600)
    assert (acme['volumeSnapshotCount'], acme['volumeSnapshotGiB']) == (1, 200)
    assert acme['vmSnapshotCount'] == 1
    assert (acme['publicIps'], acme['vpcCount'], acme['subnetCount'], acme['securityGroupCount']) == (1, 1, 2, 0)


def test_the_operator_sees_how_much_of_it_is_backups(cluster):
    """Whatever the account's setting: deciding not to bill backups needs the figure."""
    snap = cluster()

    assert (account_of(snap, 'Acme')['backupSnapshotCount'], account_of(snap, 'Acme')['backupSnapshotGiB']) == (1, 200)
    assert account_of(snap, 'Beta')['backupSnapshotGiB'] == 0
    assert snap['totals']['backupSnapshotGiB'] == 200


def test_one_account_never_carries_another_account_s_resources(cluster):
    """The boundary the whole panel rests on, stated as arithmetic."""
    snap = cluster()
    beta = account_of(snap, 'Beta')

    assert (beta['volumeCount'], beta['storageGiB']) == (1, 100)
    assert beta['securityGroupCount'] == 1
    assert account_of(snap, 'Acme')['securityGroupCount'] == 0


def test_storage_is_the_volumes_not_the_machines_root_disks(cluster):
    """A VM reports its root disk only; the pool carries the volumes."""
    acme = account_of(cluster(), 'Acme')

    assert sum(vm['diskGB'] for vm in VMS if vm['projectId'].startswith('p-a')) == 100
    assert acme['storageGiB'] == 600


def test_storage_is_split_by_medium(cluster):
    """SSD and HDD are different money; one 'storage' number hides that."""
    by_media = {row['media']: row['totalGiB'] for row in account_of(cluster(), 'Acme')['storageByMedia']}

    assert by_media == {'SSD': 100, 'HDD': 500}


def test_the_footprint_is_also_broken_down_per_project(cluster):
    snap = cluster()

    assert project_of(snap, 'Acme', 'a-main')['storageGiB'] == 550
    assert project_of(snap, 'Acme', 'a-lab')['storageGiB'] == 50
    assert project_of(snap, 'Acme', 'a-lab')['vmCount'] == 1


def test_a_row_in_an_unknown_project_is_named_not_dropped(cluster):
    """It still runs on the cluster, so it counts in the totals — and is flagged."""
    snap = cluster()

    assert snap['unattributed']['vms'] == 1
    assert snap['totals']['vcpus'] == 15
    assert sum(a['vcpus'] for a in snap['accounts']) == 14


def test_a_list_that_could_not_be_read_is_none_rather_than_zero(cluster):
    """The mutation that matters: `except: return []` would bill every client for nothing."""
    snap = cluster(broken=['list_volumes'])
    acme = account_of(snap, 'Acme')

    assert snap['unavailable'] == ['volumes']
    assert acme['storageGiB'] is None
    assert acme['volumeCount'] is None
    assert project_of(snap, 'Acme', 'a-main')['storageGiB'] is None
    assert snap['totals']['storageGiB'] is None

    # Everything else still answers.
    assert acme['vmCount'] == 2
    assert acme['volumeSnapshotGiB'] == 200


def test_one_refused_list_does_not_take_the_others_with_it(cluster):
    snap = cluster(broken=['list_vm_snapshots', 'list_subnets'])

    assert snap['unavailable'] == ['vmSnapshots', 'subnets']
    assert account_of(snap, 'Acme')['storageGiB'] == 600


def test_an_account_whose_projects_cannot_be_listed_reads_unknown_not_empty(cluster, monkeypatch):
    """Otherwise a directory hiccup makes a paying client look like an idle one."""
    def _refuse_beta(domain_id):
        if domain_id == 'dom-b':
            raise ZadaraError('forbidden', 'not readable', 403)

        return [dict(p) for p in PROJECTS[domain_id]]

    cluster()
    monkeypatch.setattr(snapshot.zadara_service, 'list_domain_project_details', _refuse_beta)
    snap = snapshot.get(refresh=True)

    beta = account_of(snap, 'Beta')
    assert snap['unreadableAccounts'] == ['Beta']
    assert (beta['vmCount'], beta['vcpus'], beta['storageGiB']) == (None, None, None)
    assert beta['projects'] == []

    # Its resources are still on the cluster, so they show up as unattributed.
    assert snap['unattributed']['volumes'] == 1
    assert account_of(snap, 'Acme')['storageGiB'] == 600


def test_without_machines_the_last_good_snapshot_is_served(cluster):
    """Compute is the spine of every screen; a blank one is worse than an old one."""
    good = cluster()
    stale = cluster(broken=['list_vms'])

    assert stale['stale'] is True
    assert account_of(stale, 'Acme')['storageGiB'] == account_of(good, 'Acme')['storageGiB']
