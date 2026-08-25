"""
The default price list.

The failure this guards against happened on the real deployment: with no tariff
in the database every resource came out "counted but not charged", and the bill
was quietly lower than the usage behind it. Nothing errored — a missing price is
indistinguishable from a deliberate zero unless someone reads the warning.

So two properties matter. Every billable resource must carry a price, which is
what breaks the moment a new one is added to `Resource`. And it must never
overwrite a list someone set on purpose, because prices are what money was
charged against.
"""

from datetime import date, datetime, timezone

import pytest

from apps.billing import rates as rate_engine
from apps.billing.management.commands import ensure_default_tariff as seeding
from apps.billing.models import Resource, Tariff, TariffRate, UsageSnapshot


def test_every_billable_resource_has_a_default_price():
    """Add a resource to `Resource` and this fails until it is priced."""
    assert {str(r) for r in seeding.PRICES} == {resource.value for resource in Resource}


def test_no_price_is_zero_or_negative():
    """A zero here would read on an invoice as a decision, not as a gap."""
    assert all(price > 0 for price in seeding.PRICES.values())


@pytest.mark.django_db
def test_an_empty_installation_gets_charged_for_everything():
    created, _ = seeding.ensure()
    assert created

    tariff = rate_engine.resolve_tariff('any-account')
    assert tariff is not None, 'an account with no list of its own must fall back to the default'

    snapshot = UsageSnapshot(
        taken_on=date(2026, 8, 1),
        taken_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        vcpus=2,
        ram_mb=4096,
        ssd_gib=50,
        hdd_gib=10,
        elastic_ips=1,
        snapshot_gib=5,
    )
    costed = rate_engine.cost_of([snapshot], rate_engine.rate_map(tariff))

    assert costed['lines'], 'a measured snapshot must produce lines'
    unpriced = [line['resource'] for line in costed['lines'] if not line['priced']]
    assert unpriced == [], f'measured but not charged: {unpriced}'
    assert costed['total'] > 0


@pytest.mark.django_db
def test_an_existing_list_is_never_touched():
    """The whole point of acting only on an empty table."""
    mine = Tariff.objects.create(name='Mine', currency='USD', account='', is_active=True)
    TariffRate.objects.create(tariff=mine, resource=Resource.VCPU, price_per_month=7)

    created, message = seeding.ensure()

    assert not created, message
    assert Tariff.objects.count() == 1
    mine.refresh_from_db()
    assert mine.currency == 'USD'
    assert [(r.resource, float(r.price_per_month)) for r in mine.rates.all()] == [(Resource.VCPU, 7.0)]


@pytest.mark.django_db
def test_running_it_twice_does_not_duplicate():
    seeding.ensure()
    seeding.ensure()

    assert Tariff.objects.count() == 1
    assert TariffRate.objects.count() == len(seeding.PRICES)
