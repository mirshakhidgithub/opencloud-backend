"""
Building and issuing an invoice.

A draft is computed from the stored quantities every time it is asked for, so it
tracks price corrections. Issuing copies it into `Invoice`/`InvoiceLine` and
from then on the document is frozen — see `Invoice`'s docstring for why.

Billing is monthly: a period is charged at the full monthly price for the average
amount held, never divided by the days that happened to be sampled.

VAT is handled in one place here. `BILLING_PRICES_INCLUDE_VAT` says which side of
the tax the tariff is on: when the prices already contain it the tax is
extracted from the total (total ÷ (1 + rate) is the base), when they do not it is
added on top. Guessing this wrong misstates every invoice by 12%, so it is
configuration rather than an assumption buried in a formula.
"""

import calendar
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db import IntegrityError, transaction

from . import rates as rate_engine
from .models import BillingProfile, Invoice, InvoiceLine, Resource, UsageSnapshot

_CENT = Decimal('0.01')

# What the quantity column counts. The price beside it is per one of these per
# month, so `quantity × price` is the line — nothing else to reconcile.
UNIT = {
    Resource.VCPU: 'vCPU',
    Resource.RAM_GB: 'ГБ',
    Resource.SSD_GB: 'ГБ',
    Resource.HDD_GB: 'ГБ',
    Resource.ELASTIC_IP: 'адрес',
    Resource.SNAPSHOT_GB: 'ГБ',
}


def _money(value: Decimal) -> Decimal:
    return value.quantize(_CENT, ROUND_HALF_UP)


def vat_rate() -> Decimal:
    try:
        return Decimal(str(settings.BILLING_VAT_RATE))
    except Exception:
        return Decimal(0)


def split_vat(gross_or_net: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    """(subtotal, vat, total) for an amount, according to the configured side."""
    rate = vat_rate() / Decimal(100)

    if not rate:
        amount = _money(gross_or_net)

        return amount, Decimal('0.00'), amount

    if settings.BILLING_PRICES_INCLUDE_VAT:
        total = _money(gross_or_net)
        subtotal = _money(total / (Decimal(1) + rate))

        return subtotal, _money(total - subtotal), total

    subtotal = _money(gross_or_net)
    vat = _money(subtotal * rate)

    return subtotal, vat, _money(subtotal + vat)


def seller() -> dict:
    return dict(settings.BILLING_SELLER)


def buyer(account: str) -> dict:
    profile = BillingProfile.objects.filter(account__iexact=account).first()

    return {
        'account': account,
        'name': (profile.legal_name if profile else '') or account,
        'taxId': profile.tax_id if profile else '',
        'address': profile.address if profile else '',
        'contract': profile.contract if profile else '',
        'director': profile.director if profile else '',
        'email': profile.email if profile else '',
        'phone': profile.phone if profile else '',
    }


def missing_requisites(seller_details: dict, buyer_details: dict) -> list[str]:
    """What the document still lacks to be a valid счёт-фактура.

    Reported rather than blocking: a draft is worth looking at before the
    paperwork is complete, and the page can say exactly what to fill in.
    """
    missing = []

    for key, label in (
        ('name', 'seller name'),
        ('taxId', 'seller ИНН'),
        ('address', 'seller address'),
        ('bankAccount', 'seller bank account'),
    ):
        if not (seller_details.get(key) or '').strip():
            missing.append(label)

    for key, label in (('name', 'buyer name'), ('taxId', 'buyer ИНН')):
        if not (buyer_details.get(key) or '').strip():
            missing.append(label)

    return missing


def draft(account: str, domain_id: str, first, last, period: str) -> dict:
    """The invoice as it would be issued today, computed from stored quantities.

    The month is charged in full — see `rates` — so a line is simply the average
    held times the monthly price, and the document **multiplies out on the page**:
    a счёт-фактура is checked by multiplying the columns. The subtotal is the sum
    of the rows shown, so the arithmetic on paper closes exactly.

    The measured-day count travels with it. It no longer divides the money, but
    it says how well the month was sampled, and an invoice built from one day of
    measurement deserves to say so.
    """
    tariff = rate_engine.resolve_tariff(account)
    rate_map = rate_engine.rate_map(tariff)

    snapshots = list(UsageSnapshot.objects.filter(domain_id=domain_id, taken_on__gte=first, taken_on__lte=last))
    cost = rate_engine.cost_of(snapshots, rate_map)

    days_in_period = (last - first).days + 1
    measured_days = cost['days']

    lines = [
        {
            'resource': line['resource'],
            'label': Resource(line['resource']).label,
            'unit': UNIT.get(line['resource'], ''),
            'quantity': line['quantity'],
            'unitDays': line['unitDays'],
            'days': measured_days,
            'daysInPeriod': days_in_period,
            'unitPrice': line['unitPrice'],
            'amount': line['cost'],
            'priced': line['priced'],
        }
        for line in cost['lines']
    ]

    billable_sum = sum((Decimal(str(line['amount'])) for line in lines if line['priced']), Decimal(0))
    subtotal, vat, total = split_vat(billable_sum)
    seller_details = seller()
    buyer_details = buyer(account)

    return {
        'status': 'draft',
        'number': None,
        'issuedAt': None,
        'account': account,
        'period': period,
        'periodFrom': str(first),
        'periodTo': str(last),
        'currency': tariff.currency if tariff else 'UZS',
        'lines': lines,
        'subtotal': float(subtotal),
        'vatRate': float(vat_rate()),
        'vatAmount': float(vat),
        'vatIncludedInPrices': bool(settings.BILLING_PRICES_INCLUDE_VAT),
        'total': float(total),
        'seller': seller_details,
        'buyer': buyer_details,
        'daysMeasured': measured_days,
        'daysInPeriod': days_in_period,
        'missingRequisites': missing_requisites(seller_details, buyer_details),
        'unpriced': [line['label'] for line in lines if not line['priced']],
    }


def serialize(invoice: Invoice) -> dict:
    """An issued invoice in the same shape as a draft, so one view renders both."""
    return {
        'status': 'issued',
        'number': invoice.number,
        'issuedAt': invoice.issued_at.isoformat(),
        'issuedBy': invoice.issued_by,
        'account': invoice.account,
        'period': invoice.period,
        'periodFrom': str(invoice.period_from),
        'periodTo': str(invoice.period_to),
        'currency': invoice.currency,
        'lines': [
            {
                'resource': line.resource,
                'label': line.label,
                'unit': line.unit,
                'quantity': float(line.quantity),
                'unitDays': None,
                'days': invoice.days_measured,
                'daysInPeriod': invoice.days_in_period,
                'unitPrice': float(line.unit_price),
                'amount': float(line.amount),
                'priced': True,
            }
            for line in invoice.lines.all()
        ],
        'subtotal': float(invoice.subtotal),
        'vatRate': float(invoice.vat_rate),
        'vatAmount': float(invoice.vat_amount),
        'vatIncludedInPrices': bool(settings.BILLING_PRICES_INCLUDE_VAT),
        'total': float(invoice.total),
        'seller': invoice.seller,
        'buyer': invoice.buyer,
        'daysMeasured': invoice.days_measured,
        'daysInPeriod': invoice.days_in_period,
        'missingRequisites': [],
        'unpriced': [],
    }


def next_number(year: int) -> tuple[int, str]:
    """Sequential within the year, which is how these documents are numbered."""
    last = Invoice.objects.filter(number__startswith=f'СФ-{year}-').order_by('-number_seq').first()
    seq = (last.number_seq if last else 0) + 1

    return seq, f'СФ-{year}-{seq:04d}'


@transaction.atomic
def issue(account: str, domain_id: str, first, last, period: str, issued_by: str) -> dict:
    """Freeze the draft into a numbered document. One invoice per account per month."""
    existing = Invoice.objects.filter(account__iexact=account, period=period).first()
    if existing:
        return serialize(existing)

    prepared = draft(account, domain_id, first, last, period)

    # Only priced lines go on the document: charging 0 for something looks like a
    # decision, while leaving it off and warning on screen is honest about the gap.
    billable = [line for line in prepared['lines'] if line['priced']]

    fields = {
        'account': account,
        'domain_id': domain_id,
        'period': period,
        'period_from': first,
        'period_to': last,
        'issued_by': issued_by,
        'currency': prepared['currency'],
        'subtotal': Decimal(str(prepared['subtotal'])),
        'vat_rate': Decimal(str(prepared['vatRate'])),
        'vat_amount': Decimal(str(prepared['vatAmount'])),
        'total': Decimal(str(prepared['total'])),
        'seller': prepared['seller'],
        'buyer': prepared['buyer'],
        'days_measured': prepared['daysMeasured'],
        'days_in_period': prepared['daysInPeriod'],
    }

    # The number is claimed by the unique constraint rather than by a lock, and
    # a clash simply takes the next one. Two administrators issuing at the same
    # instant is rare; two invoices sharing a number would not be forgivable.
    invoice = None
    for _attempt in range(5):
        seq, number = next_number(first.year)
        try:
            with transaction.atomic():
                invoice = Invoice.objects.create(number=number, number_seq=seq, **fields)
            break
        except IntegrityError:
            continue

    if invoice is None:
        raise IntegrityError('could not allocate an invoice number')

    InvoiceLine.objects.bulk_create(
        InvoiceLine(
            invoice=invoice,
            resource=line['resource'],
            label=line['label'],
            unit=line['unit'],
            quantity=Decimal(str(line['quantity'])),
            unit_price=Decimal(str(line['unitPrice'])),
            amount=Decimal(str(line['amount'])),
        )
        for line in billable
    )

    return serialize(invoice)


def month_bounds(period: str) -> tuple[date, date]:
    year, month = (int(part) for part in period.split('-', 1))
    first = date(year, month, 1)

    return first, first.replace(day=calendar.monthrange(year, month)[1])
