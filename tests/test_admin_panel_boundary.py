"""
A customer cannot reach the operator's panel.

The panel runs as a second Django process on its own domain with
`config.urls_admin`, its own session cookie name and its own authenticator, so a
customer is refused three times over: the cabinet process has no platform
routes, the admin process has no cabinet routes, and a cabinet session
authenticates as nobody there — no Zadara role can change that, because the
panel's identities live in a table the cloud cannot write to.

Asserted from the outside, because every one of these failures is invisible:
nothing errors, someone simply holds an authority they should not.

The cabinet's own `/admin/*` is a different thing and stays as it is — an
administrator of ONE account, doing account-level work for their own account.
The tests at the bottom are that boundary: its writes reach the caller's account
and nothing else, and in particular not the shared default price list that every
other account is billed by.
"""

import pytest

from datetime import date

from apps.accounts.roles import ADMIN, USER
from apps.billing.models import BillingProfile, Invoice, Tariff, TariffRate, UsageSnapshot


# --- the panel is not on the cabinet, and the cabinet is not on the panel ---


@pytest.mark.parametrize(
    'path',
    [
        '/api/v1/platform/billing',
        '/api/v1/platform/accounts',
        '/api/v1/platform/users',
        '/api/v1/platform/tariffs',
        '/api/v1/platform/auth/login',
    ],
)
def test_the_cabinet_process_does_not_serve_the_panel(client, db, path):
    """Not 403 — absent. A mistake in nginx can only ever produce a 404 here."""
    assert client.get(path).status_code == 404


@pytest.mark.urls('config.urls_admin')
@pytest.mark.parametrize(
    'path',
    [
        '/api/v1/admin/billing',
        '/api/v1/admin/tariffs',
        '/api/v1/admin/users',
        '/api/v1/user/vms',
        '/api/v1/auth/login',
    ],
)
def test_the_panel_process_does_not_serve_the_cabinet(client, db, path):
    """The other direction matters too: an operator must not act as a tenant."""
    assert client.get(path).status_code == 404


@pytest.mark.urls('config.urls_admin')
def test_a_cabinet_session_authenticates_as_nobody_on_the_panel(client, signed_in):
    """The cookie is the customer's own, valid, and for an account ADMIN."""
    signed_in(account='Acme', role=ADMIN)

    assert client.get('/api/v1/platform/billing').status_code == 403


@pytest.mark.urls('config.urls_admin')
def test_an_operator_row_with_the_same_email_does_not_lend_its_authority(client, signed_in, db):
    """A cabinet identity is not matched up to an operator by name or address.

    The panel resolves its own session key against `platform_admins`; nothing
    joins the two tables, and this is what stops a customer whose email happens
    to match an operator's from inheriting it.
    """
    from apps.platform_admin.models import FINANCE, PlatformAdmin

    PlatformAdmin.objects.create(email='someone@opencloud.uz', name='Operator', role=FINANCE)
    signed_in(account='Acme', role=ADMIN)

    assert client.get('/api/v1/platform/billing').status_code == 403


def test_an_operator_session_key_is_not_a_cabinet_session(client, db):
    """The reverse replay: the panel's session key on the cabinet's endpoints."""
    from apps.platform_admin.authentication import SESSION_KEY
    from apps.platform_admin.models import OWNER, PlatformAdmin

    admin = PlatformAdmin.objects.create(email='owner@opencloud.uz', name='Owner', role=OWNER)

    session = client.session
    session[SESSION_KEY] = admin.pk
    session.save()

    assert client.get('/api/v1/admin/billing').status_code == 403


# --- the cabinet's admin reaches its own account and no further -------------


@pytest.fixture
def priced(db, monkeypatch):
    # The admin paths resolve the caller's account against the cloud directory;
    # patched at the Zadara client boundary, as everywhere in this suite.
    from apps.integrations.zadara import service

    monkeypatch.setattr(service, 'resolve_domain_id', lambda account: {'Acme': 'dom-acme'}.get(account))

    tariff = Tariff.objects.create(name='Default price list', account='', currency='UZS')
    TariffRate.objects.create(tariff=tariff, resource='vcpu', price_per_month=46000)

    return tariff


def test_an_account_admin_prices_their_own_account_only(client, signed_in, priced):
    """The shared default is what every account without its own list is billed
    by, so one customer's administrator must never be able to move it."""
    signed_in(account='Acme', role=ADMIN)

    response = client.put(
        '/api/v1/admin/tariffs',
        {'currency': 'UZS', 'rates': {'vcpu': 1000}},
        content_type='application/json',
    )

    assert response.status_code == 200
    assert float(Tariff.objects.get(account='').rates.get(resource='vcpu').price_per_month) == 46000, 'default intact'
    assert float(Tariff.objects.get(account='Acme').rates.get(resource='vcpu').price_per_month) == 1000


def test_an_account_admin_issues_for_their_own_account_only(client, signed_in, priced, settings):
    """The document is drawn for the session's account; nothing in the request
    names one, so there is nothing to point at somebody else."""
    settings.BILLING_SELLER = {
        'name': 'OpenCloud',
        'taxId': '987654321',
        'address': 'Tashkent',
        'bankAccount': '2020',
    }
    BillingProfile.objects.create(account='Acme', legal_name='Acme LLC', tax_id='123456789')
    UsageSnapshot.objects.create(
        taken_on=date(2026, 8, 1),
        taken_at='2026-08-01T12:00:00Z',
        account='Acme',
        domain_id='dom-acme',
        project_id='p-a1',
        project_name='main',
        vcpus=4,
    )
    signed_in(account='Acme', role=ADMIN)

    response = client.post(
        '/api/v1/admin/billing/invoice', {'period': '2026-08'}, content_type='application/json'
    )

    assert response.status_code == 200
    assert [invoice.account for invoice in Invoice.objects.all()] == ['Acme']


def test_a_plain_user_reaches_none_of_it(client, signed_in, priced):
    """`/admin/*` is the account administrator's; an ordinary member has no part
    of it, which is the check that `resolve_app_role` exists to feed."""
    signed_in(account='Acme', role=USER)

    assert client.get('/api/v1/admin/billing').status_code == 403
    assert client.get('/api/v1/admin/tariffs').status_code == 403
    assert client.put('/api/v1/admin/tariffs', {'rates': {}}, content_type='application/json').status_code == 403
