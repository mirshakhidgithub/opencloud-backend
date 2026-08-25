"""
The default price list, applied on every start of the web container.

An empty tariff table is not neutral. Usage is measured either way, every
resource comes out "counted but not charged", and the bill is quietly lower than
what was actually used — nothing errors, because a missing price is
indistinguishable from a deliberate zero unless someone reads the warning. A
period that has been invoiced cannot be re-priced, so the first month of a
deployment that nobody remembered to price is simply lost.

Hence: applied automatically, not documented as a step to remember. It runs only
when the tariff table is EMPTY — an existing list, the default or any account's
own, is never touched, so it cannot overwrite a price set on purpose. Changing
prices afterwards stays a `set_tariff` job.

Deliberately NOT a data migration: prices would then seed the test database too
and quietly become the baseline every billing test is measured against.
"""

from django.core.management.base import BaseCommand

from apps.billing.models import Resource, Tariff, TariffRate

CURRENCY = 'UZS'

# Per unit per MONTH. Confirmed 2026-08-25.
PRICES = {
    Resource.VCPU: 46000,
    Resource.RAM_GB: 12600,
    Resource.SSD_GB: 1900,
    Resource.HDD_GB: 500,
    Resource.ELASTIC_IP: 50000,
    Resource.SNAPSHOT_GB: 500,
}


def ensure() -> tuple[bool, str]:
    """Returns (created, message). Safe to call on every start."""
    if Tariff.objects.exists():
        return False, 'a price list already exists — left alone'

    tariff = Tariff.objects.create(name='Default price list', currency=CURRENCY, account='', is_active=True)
    TariffRate.objects.bulk_create(
        TariffRate(tariff=tariff, resource=resource, price_per_month=price)
        for resource, price in PRICES.items()
    )

    return True, f'default price list created ({CURRENCY}): {len(PRICES)} resources priced'


class Command(BaseCommand):
    help = 'Create the default price list if no tariff exists at all.'

    def handle(self, *args, **options):
        created, message = ensure()
        self.stdout.write(self.style.SUCCESS(message) if created else message)
