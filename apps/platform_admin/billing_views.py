"""
Money, across every account (`/api/v1/platform/billing*`).

Nothing here computes a price. The cabinet already turns measurements into money
in `apps.billing` — average held × the monthly rate, VAT on the configured side,
invoices frozen once issued — and that arithmetic is covered by tests that were
checked with mutations. These views only widen its audience: the same engine,
called for an account the operator names instead of the account the caller
happens to belong to.

Two properties worth stating, because they shaped the code:

* **No cloud call.** Every figure comes from `usage_snapshots`, which is our own
  table. A bill has to be answerable on the day Zadara is unreachable, and the
  measurement it rests on was taken when the cloud was up.
* **Reading is for every operator, writing money is not.** `CanWriteBilling` is
  OWNER or FINANCE — OPS can suspend an account but must not issue its invoice.
"""

import calendar
from datetime import date
from decimal import Decimal

from rest_framework.response import Response

from apps.accounts import backups
from apps.billing import invoices as invoice_engine
from apps.billing import rates as rate_engine
from apps.billing.models import BillingProfile, Invoice, Resource, Tariff, TariffRate, UsageSnapshot
from apps.common.exceptions import AppError
from apps.common.pagination import EnvelopePagination

from . import services
from .authentication import PlatformAPIView
from .models import AdminAction
from .permissions import CanWriteBilling, IsPlatformAdmin


class _ReadAnyWriteBilling:
    """Reading is every operator's; changing money is OWNER's or FINANCE's.

    Both live on one endpoint because they are one screen — the price list you
    are looking at is the one you edit. DRF resolves permissions per view rather
    than per method, so the split is made here.
    """

    def get_permissions(self):
        if self.request.method in {'PUT', 'POST', 'PATCH', 'DELETE'}:
            return [CanWriteBilling()]

        return [IsPlatformAdmin()]


def _period(params) -> tuple[date, date, str]:
    """The month being asked about. `?period=YYYY-MM`, else the current one."""
    raw = (params.get('period') or '').strip()

    if not raw:
        today = date.today()
        raw = f'{today.year:04d}-{today.month:02d}'

    try:
        first, last = invoice_engine.month_bounds(raw)
    except (ValueError, TypeError, calendar.IllegalMonthError):
        raise AppError(message='period must look like 2026-08', code='invalid_request', status_code=400)

    return first, last, f'{first.year:04d}-{first.month:02d}'


class _Tariffs:
    """Every active price list, loaded once.

    The overview prices twenty-one accounts in a row. `resolve_tariff` per
    account would be two queries each; the resolution rule — the account's own
    list, else the shared default — is applied here against one prefetched read
    instead. Same rule, one query.
    """

    def __init__(self):
        listed = list(Tariff.objects.filter(is_active=True).prefetch_related('rates'))
        self._own = {tariff.account.lower(): tariff for tariff in listed if tariff.account}
        self._default = next((tariff for tariff in listed if not tariff.account), None)

    def of(self, account: str) -> tuple[Tariff | None, dict]:
        tariff = self._own.get(account.lower()) or self._default

        return tariff, rate_engine.rate_map(tariff)


def _tariff_payload(tariff: Tariff | None, rates: dict, account: str) -> dict:
    if not tariff:
        return {'name': None, 'currency': None, 'inherited': True, 'configured': False, 'pricedResources': 0}

    return {
        'name': tariff.name,
        'currency': tariff.currency,
        # Whether this account is riding on the shared default list. An operator
        # about to change a price needs to know whose price it is.
        #
        # `not account` is the unattributed bucket — measurements whose project
        # could not be placed in any account. Its empty name matches the shared
        # list's own empty `account`, which would otherwise read as "has a price
        # list of its own" and send an operator looking for a list nobody wrote.
        'inherited': not account or tariff.account.lower() != account.lower(),
        'configured': bool(rates),
        'pricedResources': len(rates),
    }


class BillingOverviewView(PlatformAPIView):
    """
    GET /platform/billing?period=YYYY-MM — what every account owes this month.

    Sorted by money, because that is the order the question is asked in. Each row
    carries what would stop it being invoiced — no price list, unpriced
    resources, missing requisites — so the operator sees the work before the
    month ends rather than when the document is refused.
    """

    permission_classes = [IsPlatformAdmin]

    def get(self, request):
        first, last, label = _period(request.query_params)

        snapshots = list(UsageSnapshot.objects.filter(taken_on__gte=first, taken_on__lte=last).order_by('taken_on'))

        # Grouped by DOMAIN, not by the name written at capture time: an account
        # renamed mid-month has rows under both names, and splitting it into two
        # rows here would disagree with its own page — which groups by domain —
        # and offer two invoices for one month. The label is the newest name.
        #
        # An unattributed measurement (a project whose owner the service account
        # could not see) has no domain, and is kept under its own heading rather
        # than dropped or spread over the others.
        by_account: dict[str, list] = {}
        for snapshot in snapshots:
            key = snapshot.domain_id or f'name:{snapshot.account}'
            by_account.setdefault(key, []).append(snapshot)

        tariffs = _Tariffs()
        issued = {
            invoice.account.lower(): invoice
            for invoice in Invoice.objects.filter(period=label)
        }
        seller = invoice_engine.seller()

        rows = []
        for account_snapshots in by_account.values():
            # `order_by('taken_on')` above makes the last row the newest, which
            # is the spelling to show and the one an invoice would be issued to.
            account = account_snapshots[-1].account
            tariff, rates = tariffs.of(account)
            cost = rate_engine.cost_of(account_snapshots, rates)
            invoice = issued.get(account.lower())

            rows.append(
                {
                    'account': account,
                    'domainId': next((s.domain_id for s in account_snapshots if s.domain_id), ''),
                    'projects': len({s.project_id for s in account_snapshots}),
                    'daysMeasured': cost['days'],
                    'total': cost['total'],
                    'currency': tariff.currency if tariff else None,
                    'tariff': _tariff_payload(tariff, rates, account),
                    'unpriced': [line['label'] for line in cost['lines'] if not line['priced']],
                    # One query per account against a unique index, and the rule
                    # for what a счёт-фактура needs stays in the engine that
                    # enforces it rather than being restated here.
                    'missingRequisites': (
                        invoice_engine.missing_requisites(seller, invoice_engine.buyer(account)) if account else []
                    ),
                    'invoice': (
                        {
                            'number': invoice.number,
                            'total': float(invoice.total),
                            'issuedAt': invoice.issued_at.isoformat(),
                            'issuedBy': invoice.issued_by,
                        }
                        if invoice
                        else None
                    ),
                }
            )

        rows.sort(key=lambda row: -row['total'])

        return Response(
            {
                'data': {'accounts': rows, 'totals': _cluster_totals(rows)},
                'meta': {
                    'period': label,
                    'from': str(first),
                    'to': str(last),
                    'accounts': len(rows),
                    'daysMeasured': len({s.taken_on for s in snapshots}),
                    'daysInPeriod': (last - first).days + 1,
                },
            }
        )


def _cluster_totals(rows: list[dict]) -> dict:
    """The month's revenue — per currency, never as one number across several.

    Accounts can sit on different price lists, and a price list carries its own
    currency. Adding UZS to USD would produce a figure that means nothing on a
    page whose whole purpose is money, so the sum is grouped and `currency` is
    filled in only when there is exactly one.
    """
    per_currency: dict[str, Decimal] = {}
    for row in rows:
        if row['total']:
            currency = row['currency'] or '—'
            per_currency[currency] = per_currency.get(currency, Decimal(0)) + Decimal(str(row['total']))

    by_currency = sorted(
        ({'currency': currency, 'total': float(total)} for currency, total in per_currency.items()),
        key=lambda entry: -entry['total'],
    )

    return {
        'byCurrency': by_currency,
        'currency': by_currency[0]['currency'] if len(by_currency) == 1 else None,
        'total': by_currency[0]['total'] if len(by_currency) == 1 else None,
        'accountsBilled': sum(1 for row in rows if row['total']),
        'invoicesIssued': sum(1 for row in rows if row['invoice']),
    }


def _resolve_account(name: str) -> tuple[str, str]:
    """The account as billing knows it: its stored spelling and its domain id.

    Taken from the measurements themselves rather than from the cloud directory,
    so the account a bill is drawn for cannot disagree with the rows the bill is
    drawn from. An account that was never measured resolves to no domain id —
    there is nothing to invoice, but its price list and requisites can still be
    set up before its first month.
    """
    name = (name or '').strip()
    if not name:
        raise AppError(message='An account is required.', code='invalid_request', status_code=400)

    latest = UsageSnapshot.objects.filter(account__iexact=name).order_by('-taken_on').first()
    if latest:
        return latest.account, latest.domain_id

    return name, ''


class AccountBillingView(PlatformAPIView):
    """
    GET /platform/billing/<account>?period=YYYY-MM&months=6 — one client's month.

    The same breakdown the account's own administrator sees in the cabinet, plus
    what only an operator needs: the monthly history, the invoice (issued or
    draft) and the requisites behind it.
    """

    permission_classes = [IsPlatformAdmin]

    def get(self, request, account: str):
        account, domain_id = _resolve_account(account)
        first, last, label = _period(request.query_params)

        try:
            months = min(max(int(request.query_params.get('months') or 6), 1), 24)
        except ValueError:
            raise AppError(message='months must be a number', code='invalid_request', status_code=400)

        tariff, rates = _Tariffs().of(account)

        # Matched on the domain id when we know it: `account` is the name written
        # at capture time, and a renamed account would otherwise take its old
        # rows with it — or worse, another account's.
        scope = {'domain_id': domain_id} if domain_id else {'account__iexact': account}
        period_rows = list(UsageSnapshot.objects.filter(taken_on__gte=first, taken_on__lte=last, **scope))

        by_project: dict[str, list] = {}
        for snapshot in period_rows:
            by_project.setdefault(snapshot.project_id, []).append(snapshot)

        projects = []
        for project_id, rows in by_project.items():
            cost = rate_engine.cost_of(rows, rates)
            latest = max(rows, key=lambda row: row.taken_on)
            projects.append(
                {
                    'id': project_id,
                    'name': latest.project_name or project_id,
                    'total': cost['total'],
                    'days': cost['days'],
                    'lines': cost['lines'],
                    'latest': _measurement(latest),
                }
            )

        projects.sort(key=lambda row: -row['total'])

        issued = Invoice.objects.filter(account__iexact=account, period=label).first()
        document = (
            invoice_engine.serialize(issued)
            if issued
            else invoice_engine.draft(account, domain_id, first, last, label)
        )

        return Response(
            {
                'data': {
                    'account': rate_engine.cost_of(period_rows, rates),
                    'projects': projects,
                    'tariff': _tariff_payload(tariff, rates, account),
                    'history': _history(scope, rates, months),
                    'invoice': document,
                    'profile': _profile_payload(account),
                },
                'meta': {
                    'account': account,
                    'domainId': domain_id,
                    'period': label,
                    'from': str(first),
                    'to': str(last),
                    'projects': len(projects),
                    'daysMeasured': len({row.taken_on for row in period_rows}),
                    'daysInPeriod': (last - first).days + 1,
                    # Whether the snapshot line includes scheduled backups. Said
                    # out loud because a smaller line with no reason given reads
                    # as a measurement gap.
                    'countsBackups': backups.counts_backups(domain_id),
                },
            }
        )


def _measurement(snapshot: UsageSnapshot) -> dict:
    return {
        'takenOn': str(snapshot.taken_on),
        'vmsTotal': snapshot.vms_total,
        'vmsRunning': snapshot.vms_running,
        'vcpus': snapshot.vcpus,
        'ramMB': snapshot.ram_mb,
        'ssdGiB': snapshot.ssd_gib,
        'hddGiB': snapshot.hdd_gib,
        'unlabelledGiB': snapshot.unlabelled_gib,
        'elasticIps': snapshot.elastic_ips,
        'snapshotGiB': snapshot.snapshot_gib,
        'backupSnapshotGiB': snapshot.backup_snapshot_gib,
    }


def _history(scope: dict, rates: dict, months: int) -> list[dict]:
    """Monthly totals, recomputed from quantities so a fixed price fixes history."""
    buckets: dict[str, list] = {}
    for snapshot in UsageSnapshot.objects.filter(**scope).order_by('taken_on'):
        buckets.setdefault(f'{snapshot.taken_on.year:04d}-{snapshot.taken_on.month:02d}', []).append(snapshot)

    history = []
    for period, rows in sorted(buckets.items())[-months:]:
        cost = rate_engine.cost_of(rows, rates)
        history.append({'period': period, 'total': cost['total'], 'days': cost['days']})

    return history


def _profile_payload(account: str) -> dict:
    profile = BillingProfile.objects.filter(account__iexact=account).first()

    return {
        'account': account,
        'legalName': profile.legal_name if profile else '',
        'taxId': profile.tax_id if profile else '',
        'address': profile.address if profile else '',
        'contract': profile.contract if profile else '',
        'director': profile.director if profile else '',
        'email': profile.email if profile else '',
        'phone': profile.phone if profile else '',
        'exists': profile is not None,
    }


class AccountInvoiceView(PlatformAPIView):
    """
    POST /platform/billing/<account>/invoice {period} — issue the month.

    The refusals are the engine's, not this view's: incomplete requisites, a
    month with nothing measured, a month with no priced resources. They are
    repeated here rather than trusted to the browser because issuing consumes a
    number and freezes the figures — a document that cannot be a valid
    счёт-фактура must never get one.
    """

    permission_classes = [CanWriteBilling]

    def post(self, request, account: str):
        account, domain_id = _resolve_account(account)
        payload = request.data if isinstance(request.data, dict) else {}
        first, last, label = _period(payload)

        if first > date.today():
            raise AppError(message='That month has not started yet.', code='invalid_request', status_code=400)

        document = invoice_engine.draft(account, domain_id, first, last, label)

        for reason, code, message in (
            (
                document['missingRequisites'],
                'requisites_incomplete',
                'Cannot issue: missing ' + ', '.join(document['missingRequisites']),
            ),
            (
                not document['daysMeasured'],
                'nothing_to_invoice',
                'Nothing was measured in that month, so there is nothing to invoice.',
            ),
            (
                not any(line['priced'] for line in document['lines']),
                'tariff_not_configured',
                'No priced resources in that month — set a price list first.',
            ),
        ):
            if reason:
                services.record(
                    request,
                    'invoice.issue',
                    target_account=account,
                    target_type='invoice',
                    target_name=f'{account} {label}',
                    outcome=AdminAction.FAILURE,
                    error_code=code,
                )
                raise AppError(message=message, code=code, status_code=409)

        issued = invoice_engine.issue(account, domain_id, first, last, label, request.user.email)

        # Recorded in the operators' journal, not in the client's audit log: the
        # client's log names cabinet users, and an operator is not one of them.
        services.record(
            request,
            'invoice.issue',
            target_account=account,
            target_type='invoice',
            target_id=issued['number'],
            target_name=f'{account} {label}',
            detail={'total': issued['total'], 'currency': issued['currency'], 'period': label},
        )

        return Response({'data': issued, 'meta': {'account': account, 'period': label}})


class InvoiceListView(PlatformAPIView):
    """GET /platform/invoices?account=&period= — every issued document, newest first."""

    permission_classes = [IsPlatformAdmin]

    def get(self, request):
        invoices = Invoice.objects.all()

        if account := (request.query_params.get('account') or '').strip():
            invoices = invoices.filter(account__iexact=account)
        if period := (request.query_params.get('period') or '').strip():
            invoices = invoices.filter(period=period)

        invoices = invoices.order_by('-issued_at')

        paginator = EnvelopePagination()
        page = paginator.paginate_queryset(invoices, request, view=self)

        return paginator.get_paginated_response(
            [
                {
                    'number': invoice.number,
                    'account': invoice.account,
                    'period': invoice.period,
                    'issuedAt': invoice.issued_at.isoformat(),
                    'issuedBy': invoice.issued_by,
                    'currency': invoice.currency,
                    'subtotal': float(invoice.subtotal),
                    'vatAmount': float(invoice.vat_amount),
                    'total': float(invoice.total),
                    'daysMeasured': invoice.days_measured,
                    'daysInPeriod': invoice.days_in_period,
                }
                for invoice in page
            ]
        )


class AccountProfileView(_ReadAnyWriteBilling, PlatformAPIView):
    """
    GET/PUT /platform/billing/<account>/profile — the buyer's requisites.

    An operator needs this because the client usually will not fill it in: the
    fields exist in the cabinet, but the invoice that needs them is issued here,
    and a month cannot be billed to a legal entity nobody has named.
    """

    def get(self, request, account: str):
        account, _ = _resolve_account(account)

        return Response({'data': _profile_payload(account)})

    def put(self, request, account: str):
        account, _ = _resolve_account(account)
        payload = request.data if isinstance(request.data, dict) else {}

        fields = {
            'legal_name': str(payload.get('legalName') or '').strip()[:255],
            'tax_id': str(payload.get('taxId') or '').strip()[:32],
            'address': str(payload.get('address') or '').strip()[:500],
            'contract': str(payload.get('contract') or '').strip()[:120],
            'director': str(payload.get('director') or '').strip()[:255],
            'email': str(payload.get('email') or '').strip()[:254],
            'phone': str(payload.get('phone') or '').strip()[:64],
        }

        BillingProfile.objects.update_or_create(account=account, defaults=fields)
        services.record(
            request,
            'billing.profile.update',
            target_account=account,
            target_type='billing_profile',
            target_name=account,
            detail={'legalName': fields['legal_name'], 'taxId': fields['tax_id']},
        )

        return Response({'data': _profile_payload(account)})


class TariffView(_ReadAnyWriteBilling, PlatformAPIView):
    """
    GET/PUT /platform/tariffs?account= — a price list. No account means the
    shared default, which is the one the cabinet deliberately refuses to touch:
    an account's own administrator must not be able to change what everyone pays.
    """

    def get(self, request):
        account = (request.query_params.get('account') or '').strip()

        own = Tariff.objects.filter(account__iexact=account).first() if account else None
        default = Tariff.objects.filter(account='').first()
        effective = own or default

        return Response(
            {
                'data': {
                    'account': account,
                    'name': effective.name if effective else None,
                    'currency': effective.currency if effective else 'UZS',
                    'inherited': bool(account) and own is None,
                    'rates': _rate_rows(effective),
                },
                'meta': {'hasDefault': default is not None, 'lists': _tariff_index()},
            }
        )

    def put(self, request):
        payload = request.data if isinstance(request.data, dict) else {}
        account = str(payload.get('account') or '').strip()
        currency = str(payload.get('currency') or 'UZS').strip()[:8]
        prices = _validated_prices(payload.get('rates'))

        tariff, _created = Tariff.objects.update_or_create(
            account=account,
            defaults={
                'name': f'{account} price list' if account else 'Default price list',
                'currency': currency,
                'is_active': True,
            },
        )

        for resource, price in prices.items():
            TariffRate.objects.update_or_create(
                tariff=tariff, resource=resource, defaults={'price_per_month': price}
            )

        # A resource left out of the payload is cleared rather than kept: an
        # editor that silently preserves an old price is how stale bills happen.
        TariffRate.objects.filter(tariff=tariff).exclude(resource__in=prices).delete()

        services.record(
            request,
            'billing.tariff.update',
            target_account=account,
            target_type='tariff',
            target_name=tariff.name,
            detail={'currency': currency, 'rates': {resource: float(price) for resource, price in prices.items()}},
        )

        return self.get(request)


def _rate_rows(tariff: Tariff | None) -> list[dict]:
    """Every priceable resource, priced or not — a gap is what needs filling."""
    priced = {rate.resource: rate.price_per_month for rate in (tariff.rates.all() if tariff else [])}

    return [
        {
            'resource': resource.value,
            'label': resource.label,
            'pricePerMonth': float(priced.get(resource.value, 0)),
        }
        for resource in Resource
    ]


def _tariff_index() -> list[dict]:
    """Which accounts have a list of their own, so the operator can find them."""
    return [
        {'account': tariff.account, 'name': tariff.name, 'currency': tariff.currency, 'active': tariff.is_active}
        for tariff in Tariff.objects.all().order_by('account', 'name')
    ]


def _validated_prices(submitted) -> dict[str, Decimal]:
    if not isinstance(submitted, dict):
        raise AppError(
            message='rates must be an object of resource → price per month',
            code='invalid_request',
            status_code=400,
        )

    unknown = set(submitted) - {resource.value for resource in Resource}
    if unknown:
        raise AppError(
            message=f'Unknown resources: {", ".join(sorted(unknown))}',
            code='invalid_request',
            status_code=400,
        )

    prices = {}
    for resource, value in submitted.items():
        try:
            price = Decimal(str(value))
        except Exception:
            raise AppError(message=f'Price for {resource} is not a number', code='invalid_request', status_code=400)

        if price < 0:
            raise AppError(message=f'Price for {resource} is negative', code='invalid_request', status_code=400)

        prices[resource] = price

    return prices
