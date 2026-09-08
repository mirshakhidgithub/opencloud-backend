"""
The operator's side of money.

The arithmetic itself is tested in `test_billing_maths.py` and is not repeated
here. What is new — and what would fail silently — is *who* the figures belong
to and *who* is allowed to freeze them:

  * an account's bill must be drawn from that account's measurements alone;
  * issuing an invoice and setting a price are FINANCE's authority, not the same
    one that suspends an account (OPS), and not every signed-in operator's;
  * a total across accounts that sit on different currencies is not a number.
"""

from datetime import date
from decimal import Decimal

import pytest

from apps.billing.models import BillingProfile, Invoice, Resource, Tariff, TariffRate, UsageSnapshot
from apps.platform_admin.models import AdminAction, FINANCE, OPS, OWNER, SUPPORT

pytestmark = pytest.mark.urls('config.urls_admin')

PERIOD = '2026-08'
FIRST = date(2026, 8, 1)


def measure(account, domain_id, project, day, **usage):
    return UsageSnapshot.objects.create(
        taken_on=date(2026, 8, day),
        taken_at=f'2026-08-{day:02d}T12:00:00Z',
        account=account,
        domain_id=domain_id,
        project_id=project,
        project_name=f'{project}-name',
        **usage,
    )


def price(account='', currency='UZS', **rates):
    tariff = Tariff.objects.create(name=f'{account or "default"} list', account=account, currency=currency)
    for resource, amount in rates.items():
        TariffRate.objects.create(tariff=tariff, resource=resource, price_per_month=Decimal(str(amount)))

    return tariff


@pytest.fixture
def two_accounts(db):
    """Acme holds 4 vCPU for two days; Beta holds 1 for one day."""
    price(vcpu=1000, ssd_gb=10)

    measure('Acme', 'dom-a', 'p-a1', 1, vcpus=4, ssd_gib=100)
    measure('Acme', 'dom-a', 'p-a1', 2, vcpus=4, ssd_gib=100)
    measure('Acme', 'dom-a', 'p-a2', 1, vcpus=2, ssd_gib=0)
    measure('Beta', 'dom-b', 'p-b1', 1, vcpus=1, ssd_gib=10)


def test_the_overview_prices_every_account(platform_client, client, two_accounts):
    platform_client()

    body = client.get(f'/api/v1/platform/billing?period={PERIOD}').json()['data']
    rows = {row['account']: row for row in body['accounts']}

    # Acme: 4 vCPU on both days plus 2 on one = 10 vCPU-days over 2 measured
    # days = 5 average, at 1000/month. SSD: 200 GiB-days / 2 = 100, at 10.
    assert rows['Acme']['total'] == 5000 + 1000
    assert rows['Beta']['total'] == 1000 + 100
    assert rows['Acme']['projects'] == 2
    assert rows['Acme']['daysMeasured'] == 2


def test_an_account_is_billed_from_its_own_measurements_only(platform_client, client, two_accounts):
    """The boundary, as arithmetic: Beta's row must not carry Acme's vCPU."""
    platform_client()

    body = client.get(f'/api/v1/platform/billing/Beta?period={PERIOD}').json()
    assert body['data']['account']['total'] == 1100
    assert [project['id'] for project in body['data']['projects']] == ['p-b1']
    assert body['meta']['domainId'] == 'dom-b'


def test_a_renamed_account_keeps_the_rows_of_its_own_domain(platform_client, client, db):
    """`account` is the name written at capture time; the domain id is the identity.

    An account renamed mid-month has rows under both names, and both are its own.
    A different account that later takes the old name has none of them.
    """
    price(vcpu=1000)
    measure('Old Name', 'dom-a', 'p-a1', 1, vcpus=1)
    measure('New Name', 'dom-a', 'p-a1', 2, vcpus=3)
    measure('Old Name', 'dom-z', 'p-z1', 3, vcpus=9)

    platform_client()
    body = client.get(f'/api/v1/platform/billing/New%20Name?period={PERIOD}').json()

    # Both of dom-a's days, and none of dom-z's.
    assert body['data']['account']['days'] == 2
    assert body['data']['account']['total'] == 2000


def test_a_renamed_account_is_one_row_not_two(platform_client, client, db):
    """Grouping by the captured name would offer two invoices for one month."""
    price(vcpu=1000)
    measure('Old Name', 'dom-a', 'p-a1', 1, vcpus=1)
    measure('New Name', 'dom-a', 'p-a1', 2, vcpus=3)

    platform_client()
    rows = client.get(f'/api/v1/platform/billing?period={PERIOD}').json()['data']['accounts']

    assert [row['account'] for row in rows] == ['New Name'], 'the newest spelling, once'
    assert rows[0]['total'] == 2000, 'both days, one account'


def test_a_total_is_not_added_across_currencies(platform_client, client, db):
    price(vcpu=1000)
    price(account='Beta', currency='USD', vcpu=10)
    measure('Acme', 'dom-a', 'p-a1', 1, vcpus=1)
    measure('Beta', 'dom-b', 'p-b1', 1, vcpus=1)

    platform_client()
    totals = client.get(f'/api/v1/platform/billing?period={PERIOD}').json()['data']['totals']

    assert totals['total'] is None, 'one number across two currencies would be meaningless'
    assert totals['currency'] is None
    assert {entry['currency']: entry['total'] for entry in totals['byCurrency']} == {'UZS': 1000, 'USD': 10}


def test_one_currency_still_gets_one_total(platform_client, client, two_accounts):
    platform_client()
    totals = client.get(f'/api/v1/platform/billing?period={PERIOD}').json()['data']['totals']

    assert (totals['currency'], totals['total']) == ('UZS', 7100)
    assert totals['accountsBilled'] == 2


def test_the_unattributed_bucket_rides_the_shared_list_it_is_priced_by(platform_client, client, db):
    """Measurements whose project belongs to no account still get a row.

    Its account name is empty, which matches the shared list's own empty
    `account` — so the naive comparison called it a list of the account's own and
    sent the operator looking for a price list nobody ever wrote.
    """
    price(vcpu=1000)
    measure('', '', 'p-orphan', 1, vcpus=2)

    platform_client()
    rows = client.get(f'/api/v1/platform/billing?period={PERIOD}').json()['data']['accounts']
    orphan = next(row for row in rows if row['account'] == '')

    assert orphan['tariff']['inherited'] is True
    assert orphan['total'] == 2000


def test_an_unpriced_resource_is_named_not_hidden(platform_client, client, db):
    """A gap in the price list is money nobody is charging for."""
    price(vcpu=1000)
    measure('Acme', 'dom-a', 'p-a1', 1, vcpus=1, snapshot_gib=50)

    platform_client()
    row = client.get(f'/api/v1/platform/billing?period={PERIOD}').json()['data']['accounts'][0]

    assert row['unpriced'] == [Resource.SNAPSHOT_GB.label]


# --- who may touch money ---------------------------------------------------


def requisites(account='Acme'):
    BillingProfile.objects.create(account=account, legal_name='Acme LLC', tax_id='123456789')


@pytest.fixture
def ready_to_issue(db, two_accounts, settings):
    settings.BILLING_SELLER = {
        'name': 'OpenCloud',
        'taxId': '987654321',
        'address': 'Tashkent',
        'bankAccount': '2020',
    }
    requisites()


@pytest.mark.parametrize('role', [OPS, SUPPORT])
def test_issuing_is_refused_to_roles_that_do_not_hold_money(platform_client, client, ready_to_issue, role):
    """OPS can suspend an account; that is not the same authority as invoicing it."""
    platform_client(role=role)

    response = client.post('/api/v1/platform/billing/Acme/invoice', {'period': PERIOD}, content_type='application/json')

    assert response.status_code == 403
    assert not Invoice.objects.exists()


@pytest.mark.parametrize('role', [FINANCE, OWNER])
def test_finance_and_the_owner_may_issue(platform_client, client, ready_to_issue, role):
    platform_client(role=role, email=f'{role.lower()}@opencloud.uz')

    response = client.post('/api/v1/platform/billing/Acme/invoice', {'period': PERIOD}, content_type='application/json')

    assert response.status_code == 200, response.json()
    document = response.json()['data']
    assert document['status'] == 'issued'
    assert document['number'].startswith('СФ-2026-')
    assert document['total'] == 6000


def test_issuing_is_recorded_in_the_operators_journal(platform_client, client, ready_to_issue):
    platform_client()
    client.post('/api/v1/platform/billing/Acme/invoice', {'period': PERIOD}, content_type='application/json')

    entry = AdminAction.objects.get(action='invoice.issue')
    assert (entry.outcome, entry.target_account, entry.actor_email) == ('SUCCESS', 'Acme', 'finance@opencloud.uz')
    assert entry.detail['total'] == 6000


def test_a_month_without_requisites_is_refused_and_the_attempt_kept(platform_client, client, two_accounts, settings):
    """The number and the frozen figures must never go on an invalid document."""
    settings.BILLING_SELLER = {'name': '', 'taxId': '', 'address': '', 'bankAccount': ''}
    platform_client()

    response = client.post('/api/v1/platform/billing/Acme/invoice', {'period': PERIOD}, content_type='application/json')

    assert response.status_code == 409
    assert response.json()['error']['code'] == 'requisites_incomplete'
    assert not Invoice.objects.exists()
    assert AdminAction.objects.get(action='invoice.issue').outcome == 'FAILURE'


def test_a_month_with_nothing_measured_is_refused(platform_client, client, ready_to_issue):
    platform_client()

    response = client.post(
        '/api/v1/platform/billing/Acme/invoice', {'period': '2026-07'}, content_type='application/json'
    )

    assert response.status_code == 409
    assert response.json()['error']['code'] == 'nothing_to_invoice'


def test_an_issued_document_stops_following_the_price_list(platform_client, client, ready_to_issue):
    """Re-pricing corrects a draft and every history figure; it must not touch a
    document that has already gone out under a number."""
    platform_client()
    client.post('/api/v1/platform/billing/Acme/invoice', {'period': PERIOD}, content_type='application/json')

    TariffRate.objects.filter(resource=Resource.VCPU).update(price_per_month=Decimal('99999'))

    body = client.get(f'/api/v1/platform/billing/Acme?period={PERIOD}').json()['data']

    assert body['invoice']['status'] == 'issued'
    assert body['invoice']['total'] == 6000, 'the frozen document'
    assert body['account']['total'] > 6000, 'the live figure follows the new price'


# --- price lists -----------------------------------------------------------


def test_the_shared_default_list_is_editable_here(platform_client, client, db):
    """The one place it is: the cabinet refuses, because one client's admin must
    not be able to change what every other client pays."""
    platform_client()

    response = client.put(
        '/api/v1/platform/tariffs',
        {'account': '', 'currency': 'UZS', 'rates': {'vcpu': 46000, 'ram_gb': 12600}},
        content_type='application/json',
    )

    assert response.status_code == 200
    default = Tariff.objects.get(account='')
    assert {rate.resource: float(rate.price_per_month) for rate in default.rates.all()} == {
        'vcpu': 46000.0,
        'ram_gb': 12600.0,
    }
    assert AdminAction.objects.filter(action='billing.tariff.update').exists()


def test_a_price_left_out_of_the_payload_is_cleared(platform_client, client, db):
    price(vcpu=1000, ssd_gb=10)
    platform_client()

    client.put(
        '/api/v1/platform/tariffs',
        {'account': '', 'currency': 'UZS', 'rates': {'vcpu': 2000}},
        content_type='application/json',
    )

    assert {rate.resource for rate in Tariff.objects.get(account='').rates.all()} == {'vcpu'}


def test_reading_a_price_list_needs_no_billing_role(platform_client, client, db):
    price(vcpu=1000)
    platform_client(role=SUPPORT)

    response = client.get('/api/v1/platform/tariffs')

    assert response.status_code == 200
    assert response.json()['data']['rates'][0]['pricePerMonth'] >= 0


def test_changing_a_price_needs_one(platform_client, client, db):
    platform_client(role=OPS)

    response = client.put(
        '/api/v1/platform/tariffs', {'account': '', 'rates': {'vcpu': 1}}, content_type='application/json'
    )

    assert response.status_code == 403
    assert not Tariff.objects.exists()


def test_requisites_can_be_filled_in_by_the_operator(platform_client, client, two_accounts):
    """The client rarely fills these in, and the invoice that needs them is issued here."""
    platform_client()

    response = client.put(
        '/api/v1/platform/billing/Acme/profile',
        {'legalName': 'Acme LLC', 'taxId': '123456789', 'address': 'Tashkent'},
        content_type='application/json',
    )

    assert response.status_code == 200
    assert response.json()['data']['legalName'] == 'Acme LLC'
    assert BillingProfile.objects.get(account='Acme').tax_id == '123456789'


def test_an_account_never_measured_can_still_be_prepared(platform_client, client, db):
    """Its price list and requisites are set up before its first month, not after."""
    platform_client()

    response = client.get('/api/v1/platform/billing/Newcomer')

    assert response.status_code == 200
    assert response.json()['meta']['domainId'] == ''
    assert response.json()['data']['projects'] == []


def test_signing_in_is_required(client, two_accounts):
    assert client.get(f'/api/v1/platform/billing?period={PERIOD}').status_code == 403
