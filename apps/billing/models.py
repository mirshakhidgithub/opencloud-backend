"""
Billing (spec §3.8, plan §7).

Two ideas hold this together:

1. **The cloud meters nothing we can read.** Every billing/usage/metering route
   is a 404 on this deployment and the `resource_tracker` events announce that
   Zadara's own metering ran without publishing a figure. So consumption is
   OUR measurement: a daily snapshot per project. Nothing can be reconstructed
   for a day the snapshotter did not run.

2. **Snapshots store quantities, never money.** A price is a decision that gets
   corrected; a measurement is a fact that does not. Keeping only the raw
   quantities means a fixed tariff re-bills the whole history correctly, and it
   is why `UsageSnapshot` has no cost column.
"""

from django.db import models


class Resource(models.TextChoices):
    """What can carry a price. Keys are stable — they are stored in rate rows."""

    VCPU = 'vcpu', 'vCPU'
    RAM_GB = 'ram_gb', 'RAM, GB'
    SSD_GB = 'ssd_gb', 'SSD, GB'
    HDD_GB = 'hdd_gb', 'HDD, GB'
    ELASTIC_IP = 'elastic_ip', 'Elastic IP'
    SNAPSHOT_GB = 'snapshot_gb', 'Snapshot storage, GB'


class Tariff(models.Model):
    """A price list. `account` empty means it is the default for every account."""

    name = models.CharField(max_length=120)
    currency = models.CharField(max_length=8, default='UZS')

    # The plan's `tariff_assignments`, collapsed into one nullable column: an
    # account either has its own list or falls back to the default one.
    account = models.CharField(max_length=255, blank=True, default='')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'tariffs'
        ordering = ('account', 'name')

    def __str__(self) -> str:
        return f'{self.name} ({self.currency})'


class TariffRate(models.Model):
    """Price of one resource per unit per MONTH.

    Priced by the month because that is how a price list is quoted and how a
    customer checks it. The daily rate a snapshot is charged at is derived —
    monthly ÷ the number of days in THAT month — so a full month costs exactly
    the quoted price whether it has 28 days or 31. Dividing by a flat 30 would
    quietly overcharge every 31-day month by a thirtieth.
    """

    tariff = models.ForeignKey(Tariff, related_name='rates', on_delete=models.CASCADE)
    resource = models.CharField(max_length=32, choices=Resource.choices)

    # 4 decimals: the derived daily rate is this over ~30, and rounding that
    # before multiplying by a terabyte loses real money.
    price_per_month = models.DecimalField(max_digits=18, decimal_places=4, default=0)

    class Meta:
        db_table = 'tariff_rates'
        unique_together = ('tariff', 'resource')
        ordering = ('resource',)

    def __str__(self) -> str:
        return f'{self.resource}={self.price_per_month}/month'


class UsageSnapshot(models.Model):
    """What one project held on one day. Raw measurements only — see the module docstring."""

    taken_on = models.DateField(db_index=True)

    # Kept apart from `taken_on` so the sampling interval can be shortened later
    # without a migration: the day is the billing bucket, this is the fact.
    taken_at = models.DateTimeField()

    account = models.CharField(max_length=255, db_index=True)
    domain_id = models.CharField(max_length=64, blank=True, default='')
    project_id = models.CharField(max_length=64, db_index=True)
    project_name = models.CharField(max_length=255, blank=True, default='')

    vms_total = models.PositiveIntegerField(default=0)
    vms_running = models.PositiveIntegerField(default=0)

    # Charged for running machines only — a stopped machine keeps its disks,
    # which are billed separately, but releases the compute.
    vcpus = models.PositiveIntegerField(default=0)
    ram_mb = models.PositiveIntegerField(default=0)

    ssd_gib = models.PositiveIntegerField(default=0)
    hdd_gib = models.PositiveIntegerField(default=0)

    # Disks whose volume type did not say SSD or HDD. Never silently folded into
    # either — an unpriced gigabyte should be visible, not guessed at.
    unlabelled_gib = models.PositiveIntegerField(default=0)

    elastic_ips = models.PositiveIntegerField(default=0)
    snapshot_gib = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = 'usage_snapshots'
        unique_together = ('project_id', 'taken_on')
        ordering = ('-taken_on', 'project_name')

    def __str__(self) -> str:
        return f'{self.project_name or self.project_id} on {self.taken_on}'


class BillingProfile(models.Model):
    """The buyer's requisites — what a счёт-фактура must name besides the amount.

    Kept per account and separate from `Tariff`: prices change on their own
    schedule, a legal entity's details on another, and an invoice needs both.
    """

    account = models.CharField(max_length=255, unique=True)
    legal_name = models.CharField(max_length=255, blank=True, default='')
    tax_id = models.CharField('ИНН', max_length=32, blank=True, default='')
    address = models.CharField(max_length=500, blank=True, default='')
    contract = models.CharField(max_length=120, blank=True, default='')
    director = models.CharField(max_length=255, blank=True, default='')
    email = models.EmailField(blank=True, default='')
    phone = models.CharField(max_length=64, blank=True, default='')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'billing_profiles'

    def __str__(self) -> str:
        return self.legal_name or self.account


class Invoice(models.Model):
    """An issued invoice — the one place money IS stored.

    Everywhere else a total is recomputed from quantities so a corrected price
    corrects the past. An invoice is the exception, and deliberately so: once it
    has gone to a customer under a number, changing its figures silently is not
    a correction, it is a different document. Issuing therefore freezes the
    lines; re-pricing afterwards leaves them alone.
    """

    number = models.CharField(max_length=40, unique=True)

    # Sequence within the issuing year, which is what the number is built from.
    number_seq = models.PositiveIntegerField()

    account = models.CharField(max_length=255, db_index=True)
    domain_id = models.CharField(max_length=64, blank=True, default='')

    # The billed month, as YYYY-MM.
    period = models.CharField(max_length=7, db_index=True)
    period_from = models.DateField()
    period_to = models.DateField()

    issued_at = models.DateTimeField(auto_now_add=True)
    issued_by = models.CharField(max_length=255, blank=True, default='')

    currency = models.CharField(max_length=8, default='UZS')
    subtotal = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    vat_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    vat_amount = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=18, decimal_places=2, default=0)

    # Both sides as they stood when the document was issued: a customer who
    # later changes address must not have last quarter's invoices change with them.
    seller = models.JSONField(default=dict)
    buyer = models.JSONField(default=dict)

    # How many days of the month were actually measured, recorded because it is
    # the one number that explains an unexpectedly small invoice.
    days_measured = models.PositiveIntegerField(default=0)
    days_in_period = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = 'invoices'
        unique_together = ('account', 'period')
        ordering = ('-period', 'account')

    def __str__(self) -> str:
        return f'{self.number} — {self.account} {self.period}'


class InvoiceLine(models.Model):
    """One frozen line of an issued invoice."""

    invoice = models.ForeignKey(Invoice, related_name='lines', on_delete=models.CASCADE)
    resource = models.CharField(max_length=32)
    label = models.CharField(max_length=120)
    unit = models.CharField(max_length=40, blank=True, default='')
    quantity = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    unit_price = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    amount = models.DecimalField(max_digits=18, decimal_places=2, default=0)

    class Meta:
        db_table = 'invoice_lines'
        ordering = ('resource',)

    def __str__(self) -> str:
        return f'{self.label} × {self.quantity}'
