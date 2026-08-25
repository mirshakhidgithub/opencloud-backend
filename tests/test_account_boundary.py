"""
The account boundary: an administrator administers their OWN account.

This is the one rule that has already broken here. `resolve_app_role` used to
match admin hints as substrings, so `tenant_admin` — an ordinary customer's
project role — became ADMIN, and `/admin/resources` served all 155 machines
across 21 unrelated companies. These tests exist so that cannot come back
quietly.

They are written from the outside, through the HTTP layer, because the boundary
is a property of the response and not of any single function.
"""

import pytest

from apps.accounts.roles import ADMIN, USER, resolve_app_role


# --- the exact bug: role matching -----------------------------------------


@pytest.mark.parametrize(
    'roles',
    [
        ['tenant_admin'],
        ['admin'],
        ['account_admin'],
        ['msp_admin'],
        ['_member_', 'tenant_admin'],
    ],
)
def test_admin_roles_are_recognised(roles):
    assert resolve_app_role(roles) == ADMIN


@pytest.mark.parametrize(
    'roles',
    [
        ['_member_'],
        [],
        # The regression that caused the leak: a policy bundle merely CONTAINING
        # an admin word must not confer admin.
        ['policy:AdministratorAccess'],
        ['strato-policy:VMReadOnlyAccess'],
        ['policy:ViewOnlyAccess'],
        ['administrator-of-nothing'],
        ['not_admin_at_all'],
    ],
)
def test_non_admin_roles_stay_users(roles):
    assert resolve_app_role(roles) == USER


# --- the boundary, through the API ----------------------------------------

VMS = [
    {'id': 'vm-mine-1', 'name': 'mine-1', 'projectId': 'p-acme-1', 'status': 'active', 'vcpus': 2, 'ramMB': 2048},
    {'id': 'vm-mine-2', 'name': 'mine-2', 'projectId': 'p-acme-2', 'status': 'active', 'vcpus': 4, 'ramMB': 4096},
    {'id': 'vm-theirs', 'name': 'theirs', 'projectId': 'p-other-1', 'status': 'active', 'vcpus': 8, 'ramMB': 8192},
]


@pytest.fixture
def cluster(monkeypatch):
    """A cluster holding two accounts, as the service token would see it."""
    from apps.integrations.zadara import resources, service

    monkeypatch.setattr(service, 'get_service_token', lambda force=False: 'service-token')
    monkeypatch.setattr(
        service, 'resolve_domain_id', lambda account: {'Acme': 'dom-acme', 'Other': 'dom-other'}.get(account)
    )
    monkeypatch.setattr(
        service,
        'list_domain_projects',
        lambda domain_id: {'dom-acme': {'p-acme-1': 'Alpha', 'p-acme-2': 'Beta'}, 'dom-other': {'p-other-1': 'Theirs'}}[
            domain_id
        ],
    )
    monkeypatch.setattr(resources, 'list_vms', lambda token, with_disks=False: [dict(v) for v in VMS])


@pytest.mark.django_db
def test_admin_resources_shows_only_the_callers_account(client, signed_in, cluster):
    signed_in(account='Acme')

    body = client.get('/api/v1/admin/resources').json()
    names = {vm['name'] for vm in body['data']['vms']}

    assert names == {'mine-1', 'mine-2'}, 'an account must never see another account’s machines'
    assert body['meta']['total'] == 2


@pytest.mark.django_db
def test_project_id_from_another_account_returns_nothing(client, signed_in, cluster):
    """Asking for someone else's project must yield an empty list, not their data."""
    signed_in(account='Acme')

    body = client.get('/api/v1/admin/resources?project_id=p-other-1').json()

    assert body['data']['vms'] == []


@pytest.mark.django_db
def test_project_id_from_own_account_narrows(client, signed_in, cluster):
    signed_in(account='Acme')

    body = client.get('/api/v1/admin/resources?project_id=p-acme-2').json()

    assert [vm['name'] for vm in body['data']['vms']] == ['mine-2']


@pytest.mark.django_db
def test_plain_user_cannot_reach_admin_endpoints(client, signed_in, cluster):
    signed_in(account='Acme', role=USER)

    for path in ('/api/v1/admin/resources', '/api/v1/admin/users', '/api/v1/admin/tenants', '/api/v1/admin/billing'):
        assert client.get(path).status_code == 403, f'{path} must be closed to a non-admin'


@pytest.mark.django_db
def test_anonymous_is_refused(client, cluster):
    assert client.get('/api/v1/admin/resources').status_code in (401, 403)
    assert client.get('/api/v1/user/vms').status_code in (401, 403)


@pytest.mark.django_db
def test_session_without_an_account_is_refused(client, make_user, cluster):
    """An account-less session must fail closed, not fall through to everything."""
    user = make_user(username='nobody', account='')
    client.force_login(user)

    response = client.get('/api/v1/admin/resources')

    assert response.status_code == 401
    assert response.json()['error']['code'] == 'session_expired'


@pytest.mark.django_db
def test_unknown_account_is_not_treated_as_wildcard(client, signed_in, cluster):
    """An account the cloud does not know must 404, never return the cluster."""
    signed_in(account='Ghost')

    response = client.get('/api/v1/admin/resources')

    assert response.status_code == 404
    assert response.json()['error']['code'] == 'account_not_found'
