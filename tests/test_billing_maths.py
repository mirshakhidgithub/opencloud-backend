"""
Billing arithmetic.

Wrong money is the quietest kind of bug: nobody notices a total that is 12% off
until a customer does. These tests pin the properties that must hold no matter
how the code is refactored — a full month costs exactly the quoted price, every
row multiplies out, and an issued document never moves.
"""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from apps.billing import invoices as engine
from apps.billing import rates as rate_engine
from apps.billing.models import Invoice, Tariff, TariffRate, UsageSnapshot

PRICES = {'vcpu': 46000, 'ram_gb': 12600, 'ssd_gb': 1900, 'hdd_gb': 500, 'elastic_ip': 50000, 'snapshot_gb': 500}

SHAPE = dict(vcpus=1, ram_mb=1024, ssd_gib=100, hdd_gib=10, elastic_ips=1, snapshot_gib=20, vms_total=1, vms_running=1)

# What SHAPE costs for one whole month at PRICES.
EXPECTED_MONTH = 46000 + 12600 + 100 * 1900 + 10 * 500 + 50000 + 20 * 500


@pytest.fixture
def tariff(db):
    tariff = Tariff.objects.create(name='Default', currency='UZS', account='', is_active=True)
    for resource, price in PRICES.items():
        TariffRate.objects.create(tariff=tariff, resource=resource, price_per_month=price)

    return tariff


def fill_month(year, month, days, *, project='p-1', domain='dom-1', account='Acme', **shape):
    for day in range(1, days + 1):
        UsageSnapshot.objects.create(
            taken_on=date(year, month, day),
            taken_at=datetime(year, month, day, tzinfo=timezone.utc),
            account=account,
            domain_id=domain,
            project_id=project,
            project_name='Alpha',
            **(shape or SHAPE),
        )


# --- the property that matters most ---------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize('year,month,days', [(2026, 1, 31), (2026, 2, 28), (2026, 4, 30), (2024, 2, 29)])
def test_a_full_month_costs_exactly_the_quoted_price(tariff, year, month, days):
    """28, 29, 30 or 31 days: a full month is the list price, never a fraction more."""
    fill_month(year, month, days)

    cost = rate_engine.cost_of(UsageSnapshot.objects.all(), rate_engine.rate_map(tariff))

    assert cost['total'] == pytest.approx(EXPECTED_MONTH)
    assert cost['days'] == days


@pytest.mark.django_db
def test_every_row_multiplies_out(tariff):
    """A person checks an invoice by multiplying the columns. They must agree."""
    fill_month(2026, 1, 31)

    cost = rate_engine.cost_of(UsageSnapshot.objects.all(), rate_engine.rate_map(tariff))

    for line in cost['lines']:
        assert line['cost'] == pytest.approx(line['quantity'] * line['unitPrice']), line['resource']

    assert cost['total'] == pytest.approx(sum(line['cost'] for line in cost['lines']))


@pytest.mark.django_db
def test_quantity_is_the_average_not_the_sum(tariff):
    """Held for 31 days, one vCPU is one vCPU — not thirty-one."""
    fill_month(2026, 1, 31)

    cost = rate_engine.cost_of(UsageSnapshot.objects.all(), rate_engine.rate_map(tariff))
    vcpu = next(line for line in cost['lines'] if line['resource'] == 'vcpu')

    assert vcpu['quantity'] == pytest.approx(1)
    assert vcpu['unitDays'] == pytest.approx(31)


@pytest.mark.django_db
def test_a_thinly_measured_month_is_not_cheaper(tariff):
    """Billing is monthly: three sampled days bill the month, they do not discount it."""
    fill_month(2026, 1, 3)

    cost = rate_engine.cost_of(UsageSnapshot.objects.all(), rate_engine.rate_map(tariff))

    assert cost['total'] == pytest.approx(EXPECTED_MONTH)
    assert cost['days'] == 3, 'the day count is still reported, so the reader can judge the average'


@pytest.mark.django_db
def test_a_resource_without_a_price_is_counted_but_not_charged(tariff):
    TariffRate.objects.filter(tariff=tariff, resource='snapshot_gb').delete()
    fill_month(2026, 1, 31)

    cost = rate_engine.cost_of(UsageSnapshot.objects.all(), rate_engine.rate_map(tariff))
    snapshots = next(line for line in cost['lines'] if line['resource'] == 'snapshot_gb')

    assert snapshots['priced'] is False
    assert snapshots['quantity'] == pytest.approx(20), 'still measured'
    assert snapshots['cost'] == 0
    assert cost['total'] == pytest.approx(EXPECTED_MONTH - 20 * 500)


@pytest.mark.django_db
def test_no_snapshots_means_no_charge(tariff):
    cost = rate_engine.cost_of(UsageSnapshot.objects.none(), rate_engine.rate_map(tariff))

    assert cost['total'] == 0
    assert cost['lines'] == []
    assert cost['days'] == 0


# --- VAT -------------------------------------------------------------------


@pytest.mark.django_db
def test_vat_off_leaves_the_total_alone(settings):
    settings.BILLING_VAT_RATE = '0'

    subtotal, vat, total = engine.split_vat(Decimal('1000'))

    assert (subtotal, vat, total) == (Decimal('1000.00'), Decimal('0.00'), Decimal('1000.00'))


@pytest.mark.django_db
def test_vat_added_on_top_of_net_prices(settings):
    settings.BILLING_VAT_RATE = '12'
    settings.BILLING_PRICES_INCLUDE_VAT = False

    subtotal, vat, total = engine.split_vat(Decimal('1000'))

    assert subtotal == Decimal('1000.00')
    assert vat == Decimal('120.00')
    assert total == Decimal('1120.00')


@pytest.mark.django_db
def test_vat_extracted_when_prices_already_include_it(settings):
    """The opposite reading of the same rate — getting this backwards is a 12% error."""
    settings.BILLING_VAT_RATE = '12'
    settings.BILLING_PRICES_INCLUDE_VAT = True

    subtotal, vat, total = engine.split_vat(Decimal('1120'))

    assert total == Decimal('1120.00'), 'the quoted amount is what the customer pays'
    assert subtotal == Decimal('1000.00')
    assert vat == Decimal('120.00')
    assert subtotal + vat == total


# --- the estimate is read the same way as a bill ---------------------------


@pytest.mark.django_db
def test_the_estimate_rows_multiply_out_and_sum_to_its_total(tariff):
    """The Bill tab's estimate is a table people check with a calculator.

    It used to multiply the unrounded quantity while showing the rounded one, and
    to sum unrounded costs, so a row did not multiply out and the total was not
    the sum of the rows. RAM is the case that exposes it: 1536 MB is 1.5 GB
    exactly, 1500 MB is 1.4648…
    """
    rate_map = rate_engine.rate_map(tariff)
    estimate = rate_engine.estimate_month({**SHAPE, 'ram_mb': 1500}, rate_map)

    for line in estimate['lines']:
        expected = round(line['quantity'] * line['unitPrice'], 2)
        assert line['cost'] == pytest.approx(expected, abs=0.001), f'{line["resource"]} does not multiply out'

    assert estimate['total'] == pytest.approx(sum(line['cost'] for line in estimate['lines']), abs=0.001)


@pytest.mark.django_db
def test_the_estimate_agrees_with_a_month_of_that_shape(tariff):
    """Estimate and bill must not drift: the same shape, the same money."""
    rate_map = rate_engine.rate_map(tariff)
    estimate = rate_engine.estimate_month(SHAPE, rate_map)
    fill_month(2026, 6, 30)
    billed = rate_engine.cost_of(list(UsageSnapshot.objects.all()), rate_map)

    assert estimate['total'] == pytest.approx(billed['total'], abs=0.01) == pytest.approx(EXPECTED_MONTH, abs=0.01)
