"""
Scheduled backups an account is set not to count.

Some clients are backed up by us with a protection group, and an operator can
take those snapshots out of the client's view and off their bill. Three ways
that goes quietly wrong, each worse than the switch not existing:

  * the bill and the page disagree — the list hides a terabyte the invoice still
    charges for, or the other way round;
  * the switch reaches further than one account, or further than the backups —
    a client's own hand-made snapshots vanish with them;
  * the operator loses sight of what is hidden, and cannot tell a switched-off
    account from one that holds nothing.
"""

from datetime import date

import pytest

from apps.accounts import backups
from apps.accounts.models import AccountSettings
from apps.accounts.roles import ADMIN
from apps.billing import collector
from apps.billing import rates as rate_engine
from apps.billing.models import Tariff, TariffRate, UsageSnapshot
from apps.platform_admin.models import AdminAction, FINANCE, OPS, SUPPORT

# One hand-made snapshot and one a protection group took, in the same project.
MANUAL = {'id': 's-hand', 'name': 'before-upgrade', 'sizeGiB': 10, 'projectId': 'proj-1',
          'sourceVolumeId': 'v-1', 'protectionGroupId': None}
SCHEDULED = {'id': 's-pg', 'name': 'daily-0300', 'sizeGiB': 500, 'projectId': 'proj-1',
             'sourceVolumeId': 'v-1', 'protectionGroupId': 'pg-1'}

VM_MANUAL = {'id': 'vs-hand', 'name': 'manual', 'projectId': 'proj-1', 'protectionGroup': None}
VM_SCHEDULED = {'id': 'vs-pg', 'name': 'nightly', 'projectId': 'proj-1', 'protectionGroup': 'nightly-backup'}


def switch_off(domain_id='dom-acme'):
    AccountSettings.objects.create(domain_id=domain_id, account='Acme', count_backup_snapshots=False)


def measure(domain_id, day, *, snapshot_gib, backup_snapshot_gib=0, project='p-1'):
    return UsageSnapshot.objects.create(
        taken_on=date(2026, 8, day),
        taken_at=f'2026-08-{day:02d}T12:00:00Z',
        account='Acme' if domain_id == 'dom-acme' else 'Beta',
        domain_id=domain_id,
        project_id=project,
        snapshot_gib=snapshot_gib,
        backup_snapshot_gib=backup_snapshot_gib,
    )


# --- telling a backup from a snapshot -------------------------------------


def test_a_protection_group_is_what_makes_a_backup():
    assert backups.is_backup(SCHEDULED)
    assert backups.is_backup(VM_SCHEDULED)
    assert not backups.is_backup(MANUAL)
    assert not backups.is_backup(VM_MANUAL)


def test_the_daily_measurement_keeps_the_backups_apart(monkeypatch):
    """Recorded for every account, so switching later never finds a day unsplit."""
    monkeypatch.setattr(collector.zadara_resources, 'list_vms', lambda token: [])
    monkeypatch.setattr(collector.zadara_resources, 'list_volumes', lambda token: [])
    monkeypatch.setattr(collector.zadara_resources, 'list_elastic_ips', lambda token: [])
    monkeypatch.setattr(collector.zadara_resources, 'list_volume_snapshots', lambda token: [MANUAL, SCHEDULED])

    measured = collector.measure_cluster('svc-token')['proj-1']

    assert measured['snapshot_gib'] == 510, 'every snapshot is still storage held'
    assert measured['backup_snapshot_gib'] == 500


# --- the bill -------------------------------------------------------------


@pytest.fixture
def snapshot_price(db):
    tariff = Tariff.objects.create(name='Default', account='', currency='UZS')
    TariffRate.objects.create(tariff=tariff, resource='snapshot_gb', price_per_month=100)

    return rate_engine.rate_map(tariff)


def test_switched_off_backups_leave_the_bill(snapshot_price):
    switch_off()
    measure('dom-acme', 1, snapshot_gib=510, backup_snapshot_gib=500)

    cost = rate_engine.cost_of(UsageSnapshot.objects.all(), snapshot_price)

    assert cost['total'] == 10 * 100, 'the hand-made 10 GiB only'


def test_backups_are_billed_while_the_switch_is_on(snapshot_price):
    measure('dom-acme', 1, snapshot_gib=510, backup_snapshot_gib=500)

    cost = rate_engine.cost_of(UsageSnapshot.objects.all(), snapshot_price)

    assert cost['total'] == 510 * 100


def test_the_switch_belongs_to_one_account(snapshot_price):
    """Priced together — the platform overview does exactly this — and still apart."""
    switch_off('dom-acme')
    measure('dom-acme', 1, snapshot_gib=510, backup_snapshot_gib=500, project='p-a')
    measure('dom-beta', 1, snapshot_gib=510, backup_snapshot_gib=500, project='p-b')

    acme = rate_engine.cost_of(UsageSnapshot.objects.filter(domain_id='dom-acme'), snapshot_price)
    beta = rate_engine.cost_of(UsageSnapshot.objects.filter(domain_id='dom-beta'), snapshot_price)
    both = rate_engine.cost_of(UsageSnapshot.objects.all(), snapshot_price)

    assert acme['total'] == 10 * 100
    assert beta['total'] == 510 * 100
    assert both['total'] == (10 + 510) * 100


def test_a_day_measured_before_the_split_existed_bills_as_it_did(snapshot_price):
    """Its backups are unknown, not zero — so nothing is taken off it."""
    switch_off()
    measure('dom-acme', 1, snapshot_gib=510)

    cost = rate_engine.cost_of(UsageSnapshot.objects.all(), snapshot_price)

    assert cost['total'] == 510 * 100


# --- the client's cabinet --------------------------------------------------


@pytest.fixture
def acme_cloud(db, monkeypatch):
    from apps.integrations.zadara import resources, service

    monkeypatch.setattr(service, 'resolve_domain_id', lambda account: {'Acme': 'dom-acme'}.get(account))
    monkeypatch.setattr(resources, 'list_volume_snapshots', lambda token: [dict(MANUAL), dict(SCHEDULED)])
    monkeypatch.setattr(resources, 'list_vm_snapshots', lambda token: [dict(VM_MANUAL), dict(VM_SCHEDULED)])
    monkeypatch.setattr(resources, 'list_vms', lambda token, **kwargs: [])
    monkeypatch.setattr(resources, 'list_elastic_ips', lambda token: [])
    monkeypatch.setattr(
        resources,
        'list_volumes',
        lambda token: [{'id': 'v-1', 'name': 'data', 'media': 'SSD', 'volumeType': 'ssd', 'sizeGiB': 100,
                        'projectId': 'proj-1'}],
    )


def test_the_client_sees_no_backups_once_switched_off(client, signed_in, acme_cloud):
    switch_off()
    signed_in(account='Acme', role=ADMIN)

    body = client.get('/api/v1/user/snapshots').json()

    assert [s['id'] for s in body['data']['volumeSnapshots']] == ['s-hand']
    assert [s['id'] for s in body['data']['vmSnapshots']] == ['vs-hand']
    assert body['meta']['volumeSnapshots'] == 1
    assert body['meta']['vmSnapshots'] == 1
    assert body['meta']['volumeSnapshotGiB'] == 10, 'totals that never saw them'


def test_the_client_sees_every_snapshot_by_default(client, signed_in, acme_cloud):
    signed_in(account='Acme', role=ADMIN)

    body = client.get('/api/v1/user/snapshots').json()

    assert {s['id'] for s in body['data']['volumeSnapshots']} == {'s-hand', 's-pg'}
    assert body['meta']['volumeSnapshotGiB'] == 510


def test_the_live_estimate_holds_no_backups_either(client, signed_in, acme_cloud, snapshot_price):
    """The page and the bill agree: the list and 'what you hold now' hide the same thing."""
    switch_off()
    signed_in(account='Acme', role=ADMIN)

    body = client.get('/api/v1/user/billing').json()['data']

    assert body['current']['snapshot_gib'] == 10
    assert body['estimate']['total'] == 10 * 100


def test_the_client_s_own_bill_leaves_backups_off(client, signed_in, acme_cloud, snapshot_price):
    switch_off()
    UsageSnapshot.objects.create(
        taken_on=date.today(), taken_at='2026-08-01T12:00:00Z', account='Acme', domain_id='dom-acme',
        project_id='proj-1', snapshot_gib=510, backup_snapshot_gib=500,
    )
    signed_in(account='Acme', role=ADMIN)

    body = client.get('/api/v1/admin/billing').json()['data']

    assert body['account']['total'] == 10 * 100
    assert body['projects'][0]['latest']['snapshotGiB'] == 10


# --- the operator's switch -------------------------------------------------


@pytest.fixture
def acme_card(monkeypatch):
    from apps.platform_admin import snapshot, views

    monkeypatch.setattr(
        snapshot, 'account', lambda account_id: {'id': 'dom-acme', 'name': 'Acme', 'projects': []}
        if account_id == 'dom-acme' else None,
    )
    monkeypatch.setattr(views.zadara_service, 'list_domain_users', lambda account_id: [])


@pytest.mark.urls('config.urls_admin')
def test_the_card_says_backups_count_until_told_otherwise(platform_client, client, acme_card):
    platform_client(role=SUPPORT)

    settings = client.get('/api/v1/platform/accounts/dom-acme').json()['data']['settings']

    assert settings == {'countBackupSnapshots': True, 'updatedAt': None, 'updatedBy': None}


@pytest.mark.urls('config.urls_admin')
@pytest.mark.parametrize('role', [OPS, FINANCE])
def test_operations_and_finance_can_both_flip_it(platform_client, client, acme_card, role):
    """It is what the client sees and what they pay — either authority is enough."""
    platform_client(role=role, email=f'{role.lower()}@opencloud.uz')

    response = client.patch(
        '/api/v1/platform/accounts/dom-acme', {'countBackupSnapshots': False}, content_type='application/json'
    )

    assert response.status_code == 200
    assert response.json()['data']['settings']['countBackupSnapshots'] is False
    assert AccountSettings.objects.get(domain_id='dom-acme').count_backup_snapshots is False

    logged = AdminAction.objects.get(action='account.settings.update')
    assert (logged.target_id, logged.detail) == ('dom-acme', {'countBackupSnapshots': False})


@pytest.mark.urls('config.urls_admin')
def test_support_looks_but_does_not_flip(platform_client, client, acme_card):
    platform_client(role=SUPPORT)

    response = client.patch(
        '/api/v1/platform/accounts/dom-acme', {'countBackupSnapshots': False}, content_type='application/json'
    )

    assert response.status_code == 403
    assert not AccountSettings.objects.exists()


@pytest.mark.urls('config.urls_admin')
def test_a_string_false_is_not_taken_for_a_decision(platform_client, client, acme_card):
    """'false' is truthy; accepting it would switch billing the wrong way."""
    platform_client(role=OPS, email='ops@opencloud.uz')

    response = client.patch(
        '/api/v1/platform/accounts/dom-acme', {'countBackupSnapshots': 'false'}, content_type='application/json'
    )

    assert response.status_code == 400
    assert not AccountSettings.objects.exists()


@pytest.mark.urls('config.urls_admin')
def test_an_unknown_account_gets_no_settings_row(platform_client, client, acme_card):
    platform_client(role=OPS, email='ops@opencloud.uz')

    response = client.patch(
        '/api/v1/platform/accounts/dom-nobody', {'countBackupSnapshots': False}, content_type='application/json'
    )

    assert response.status_code == 404
    assert not AccountSettings.objects.exists()
