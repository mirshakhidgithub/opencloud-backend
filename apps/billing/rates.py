"""
Turning measurements into money.

The only place that knows how a stored measurement becomes a billable quantity
and how a quoted price becomes a charge, so a change of unit is one edit rather
than a hunt through views.

**Billing is monthly.** Prices are quoted per month and a month is charged in
full: what a period costs is the average amount held during it times the monthly
price. A resource that existed for part of the month is not discounted for the
rest — that is what invoicing monthly means, and it is the customer-facing rule.

The daily snapshots are therefore a way of MEASURING the month, not of dividing
it. They still matter: the average is only as good as the days sampled, so every
figure travels with the count of days behind it.
"""

from decimal import ROUND_HALF_UP, Decimal

from .models import Resource, Tariff, UsageSnapshot

# measurement → billable quantity for one day. RAM is divided by 1024 the way
# the cloud's own arithmetic does (it reports `ramMB`).
QUANTITY = {
    Resource.VCPU: lambda s: Decimal(s.vcpus),
    Resource.RAM_GB: lambda s: Decimal(s.ram_mb) / Decimal(1024),
    Resource.SSD_GB: lambda s: Decimal(s.ssd_gib),
    Resource.HDD_GB: lambda s: Decimal(s.hdd_gib),
    Resource.ELASTIC_IP: lambda s: Decimal(s.elastic_ips),
    Resource.SNAPSHOT_GB: lambda s: Decimal(s.snapshot_gib),
}

_CENT = Decimal('0.01')


def _money(value: Decimal) -> Decimal:
    return value.quantize(_CENT, ROUND_HALF_UP)


def resolve_tariff(account: str) -> Tariff | None:
    """The account's own price list, else the default one, else nothing."""
    own = Tariff.objects.filter(is_active=True, account__iexact=account).first()

    return own or Tariff.objects.filter(is_active=True, account='').first()


def rate_map(tariff: Tariff | None) -> dict[str, Decimal]:
    """resource → price per unit per MONTH. A resource with no row is not charged."""
    if not tariff:
        return {}

    return {rate.resource: rate.price_per_month for rate in tariff.rates.all() if rate.price_per_month}


def quantities(snapshot: UsageSnapshot) -> dict[str, Decimal]:
    return {resource: measure(snapshot) for resource, measure in QUANTITY.items()}


def cost_of(snapshots, rates: dict[str, Decimal]) -> dict:
    """Cost of a period under one monthly rate map.

    `quantity` is the AVERAGE amount held across the days that were measured,
    and the money is that average at the monthly price — one multiplication, so
    every line multiplies out for whoever reads it. `unitDays` keeps the raw
    measurement (a GB held three days is 3 GB-days) for anyone who wants to
    check where the average came from.

    Rounding the average before multiplying is deliberate: the figure shown and
    the figure charged must be the same one, or the columns stop adding up.
    """
    unit_days: dict[str, Decimal] = {}
    days = set()

    for snapshot in snapshots:
        days.add(snapshot.taken_on)
        for resource, quantity in quantities(snapshot).items():
            if quantity:
                unit_days[resource] = unit_days.get(resource, Decimal(0)) + quantity

    measured = Decimal(len(days)) if days else Decimal(0)

    lines = []
    total = Decimal(0)
    for resource, accumulated in sorted(unit_days.items()):
        average = (accumulated / measured).quantize(_CENT, ROUND_HALF_UP) if measured else Decimal(0)
        monthly = rates.get(resource, Decimal(0))
        cost = _money(average * monthly)
        total += cost

        lines.append(
            {
                'resource': resource,
                'label': Resource(resource).label,
                'quantity': float(average),
                'unitDays': float(accumulated.quantize(_CENT, ROUND_HALF_UP)),
                'unitPrice': float(monthly),
                'cost': float(cost),
                # A measured resource with no price is shown, not hidden: an
                # unpriced line is a gap in the tariff, and silence hides it.
                'priced': resource in rates,
            }
        )

    return {'lines': lines, 'total': float(total), 'days': len(days)}


def estimate_month(measurements: dict, rates: dict[str, Decimal]) -> dict:
    """What today's shape would cost for a month, at the quoted prices.

    Same arithmetic as a billed month with one measured day, which is the point:
    the estimate and the bill cannot drift apart. It is still an estimate — it
    assumes nothing is created or destroyed, which is never quite true.
    """
    snapshot = UsageSnapshot(**measurements)
    lines = []
    total = Decimal(0)

    for resource, quantity in sorted(quantities(snapshot).items()):
        if not quantity:
            continue

        # Round the quantity BEFORE multiplying, and sum the rounded costs — the
        # same order as a billed month. Multiplying the unrounded quantity while
        # showing the rounded one gave rows that did not multiply out, and adding
        # unrounded costs gave a total that was not the sum of the rows shown.
        held = quantity.quantize(_CENT, ROUND_HALF_UP)
        monthly = rates.get(resource, Decimal(0))
        cost = _money(held * monthly)
        total += cost
        lines.append(
            {
                'resource': resource,
                'label': Resource(resource).label,
                'quantity': float(held),
                'unitPrice': float(monthly),
                'cost': float(cost),
                'priced': resource in rates,
            }
        )

    return {'lines': lines, 'total': float(total), 'days': 0}
