"""
Billing endpoints (spec §3.8).

Two audiences, one boundary. A user sees what their current project costs; an
administrator sees the account with a per-project breakdown. Neither can reach
another account: the user view is pinned to the session's project, the admin
views to `account_domain(request)`.

Cost is always computed from stored quantities against the current tariff, never
read from a stored total — see `apps.billing.models`.
"""

import calendar
import csv
import io
from datetime import date

from django.http import HttpResponse
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.models import AuditLog
from apps.audit.services import record
from apps.authentication import vault
from apps.common.concurrency import gather
from apps.common.exceptions import AppError
from apps.common.permissions import IsAdmin
from apps.common.tenancy import account_domain
from apps.integrations.zadara import resources as zadara_resources

from . import invoices as invoice_engine
from . import rates as rate_engine
from .models import BillingProfile, Invoice, Resource, Tariff, TariffRate, UsageSnapshot

RUNNING = ('active', 'running')


def _period(params) -> tuple[date, date, str]:
    """The month being billed. `?period=YYYY-MM`, defaulting to the current one."""
    raw = (params.get('period') or '').strip()
    today = date.today()

    if not raw:
        first = today.replace(day=1)
    else:
        try:
            year, month = (int(part) for part in raw.split('-', 1))
            first = date(year, month, 1)
        except (ValueError, TypeError):
            raise AppError(message='period must look like 2026-08', code='invalid_request', status_code=400)

    last = first.replace(day=calendar.monthrange(first.year, first.month)[1])

    return first, last, f'{first.year:04d}-{first.month:02d}'


def _tariff_payload(tariff: Tariff | None, rates: dict) -> dict:
    if not tariff:
        return {'name': None, 'currency': None, 'account': None, 'configured': False, 'pricedResources': 0}

    return {
        'name': tariff.name,
        'currency': tariff.currency,
        # Empty means this account has no list of its own and uses the default.
        'account': tariff.account or None,
        'configured': bool(rates),
        'pricedResources': len(rates),
    }


def _live_measurements(token: str, project_id: str) -> tuple[dict, list[str]]:
    """Today's shape of ONE project, for the estimate. Each source degrades alone.

    Every resource is matched against `project_id` rather than trusted to be in
    scope. A project-scoped token does return only its own project — but a token
    with wider rights (an MSP role, say) returns the whole cluster, and an
    estimate that quietly priced 155 machines instead of 6 would be worse than
    no estimate at all.
    """
    values = {
        'vms_total': 0,
        'vms_running': 0,
        'vcpus': 0,
        'ram_mb': 0,
        'ssd_gib': 0,
        'hdd_gib': 0,
        'unlabelled_gib': 0,
        'elastic_ips': 0,
        'snapshot_gib': 0,
    }
    # Four independent reads for one estimate — together rather than in turn.
    results = gather(
        {
            'machines': lambda: zadara_resources.list_vms(token),
            'volumes': lambda: zadara_resources.list_volumes(token),
            'addresses': lambda: zadara_resources.list_elastic_ips(token),
            'snapshots': lambda: zadara_resources.list_volume_snapshots(token),
        }
    )
    unavailable = [name for name, result in results.items() if not result.ok]

    def mine(name):
        return [item for item in results[name].value or [] if item.get('projectId') == project_id]

    for vm in mine('machines'):
        values['vms_total'] += 1
        if vm['status'].lower() in RUNNING:
            values['vms_running'] += 1
            values['vcpus'] += vm['vcpus']
            values['ram_mb'] += vm['ramMB']

    for volume in mine('volumes'):
        key = {'SSD': 'ssd_gib', 'HDD': 'hdd_gib'}.get(volume.get('media') or '', 'unlabelled_gib')
        values[key] += volume['sizeGiB']

    for _eip in mine('addresses'):
        values['elastic_ips'] += 1

    for snapshot in mine('snapshots'):
        values['snapshot_gib'] += snapshot['sizeGiB']

    return values, unavailable


class UserBillingView(APIView):
    """
    GET /api/v1/user/billing?period=YYYY-MM — what the current project costs.

    Returns both numbers, because they answer different questions: `accrued` is
    the measured bill for the period so far, `estimate` is what today's shape
    would cost for a whole month at the quoted prices. Before the snapshotter
    has run for a while only the estimate is meaningful, and the response says
    how many days the accrued figure actually covers.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        token = vault.get(request.session.session_key) if request.session.session_key else None
        if not token:
            raise AppError(message='Session expired, please sign in again.', code='session_expired', status_code=401)

        project_id = request.session.get('zadara_project_id')
        if not project_id:
            raise AppError(
                message='This session has no project scope, so there is nothing to price.',
                code='no_project_scope',
                status_code=409,
            )

        first, last, label = _period(request.query_params)
        account = (request.user.account or '').strip()

        tariff = rate_engine.resolve_tariff(account)
        rates = rate_engine.rate_map(tariff)

        snapshots = UsageSnapshot.objects.filter(project_id=project_id, taken_on__gte=first, taken_on__lte=last)
        accrued = rate_engine.cost_of(snapshots, rates)

        measurements, unavailable = _live_measurements(token, project_id)
        estimate = rate_engine.estimate_month(measurements, rates)

        return Response(
            {
                'data': {
                    'accrued': accrued,
                    'estimate': estimate,
                    'current': measurements,
                    'tariff': _tariff_payload(tariff, rates),
                },
                'meta': {
                    'period': label,
                    'from': str(first),
                    'to': str(last),
                    'project': request.session.get('zadara_project_name'),
                    'account': account,
                    'unavailable': unavailable,
                },
            }
        )


class AdminBillingView(APIView):
    """
    GET /api/v1/admin/billing?period=YYYY-MM — the account's bill, by project.

    ADMIN only, and narrowed to the caller's own account: the snapshot table
    holds every project on the cluster, so the filter is the whole safety story
    here. Filtering on `account` alone would trust the name written at capture
    time, so it is checked against the domain id as well.
    """

    permission_classes = [IsAdmin]

    def get(self, request):
        account, domain_id = account_domain(request)
        first, last, label = _period(request.query_params)

        tariff = rate_engine.resolve_tariff(account)
        rates = rate_engine.rate_map(tariff)

        snapshots = list(
            UsageSnapshot.objects.filter(domain_id=domain_id, taken_on__gte=first, taken_on__lte=last)
        )

        by_project: dict[str, list] = {}
        for snapshot in snapshots:
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
                    'latest': {
                        'takenOn': str(latest.taken_on),
                        'vmsTotal': latest.vms_total,
                        'vmsRunning': latest.vms_running,
                        'vcpus': latest.vcpus,
                        'ramMB': latest.ram_mb,
                        'ssdGiB': latest.ssd_gib,
                        'hddGiB': latest.hdd_gib,
                        'unlabelledGiB': latest.unlabelled_gib,
                        'elasticIps': latest.elastic_ips,
                        'snapshotGiB': latest.snapshot_gib,
                    },
                }
            )

        projects.sort(key=lambda row: -row['total'])
        account_cost = rate_engine.cost_of(snapshots, rates)

        # The measured days, so a partial month is never read as a cheap one.
        days = UsageSnapshot.objects.filter(
            domain_id=domain_id, taken_on__gte=first, taken_on__lte=last
        ).values_list('taken_on', flat=True).distinct()

        return Response(
            {
                'data': {
                    'account': account_cost,
                    'projects': projects,
                    'tariff': _tariff_payload(tariff, rates),
                },
                'meta': {
                    'period': label,
                    'from': str(first),
                    'to': str(last),
                    'account': account,
                    'projects': len(projects),
                    'daysMeasured': len(set(days)),
                    'daysInPeriod': (last - first).days + 1,
                },
            }
        )


class AdminUsageExportView(APIView):
    """
    GET /api/v1/admin/billing/export?period=YYYY-MM — the same figures as CSV.

    Rows are the raw daily measurements rather than the priced summary: an
    exported bill is usually checked against something, and a quantity can be
    re-priced while a total cannot be taken apart.
    """

    permission_classes = [IsAdmin]

    def get(self, request):
        account, domain_id = account_domain(request)
        first, last, label = _period(request.query_params)

        rows = UsageSnapshot.objects.filter(
            domain_id=domain_id, taken_on__gte=first, taken_on__lte=last
        ).order_by('taken_on', 'project_name')

        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            [
                'date',
                'account',
                'project',
                'project_id',
                'vms_total',
                'vms_running',
                'vcpus',
                'ram_mb',
                'ssd_gib',
                'hdd_gib',
                'unlabelled_gib',
                'elastic_ips',
                'snapshot_gib',
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row.taken_on,
                    row.account,
                    row.project_name,
                    row.project_id,
                    row.vms_total,
                    row.vms_running,
                    row.vcpus,
                    row.ram_mb,
                    row.ssd_gib,
                    row.hdd_gib,
                    row.unlabelled_gib,
                    row.elastic_ips,
                    row.snapshot_gib,
                ]
            )

        # A plain HttpResponse, not a DRF Response: the project renderer wraps
        # every DRF body in the {data,meta} envelope, which would turn the file
        # into a JSON string containing a CSV.
        response = HttpResponse(buffer.getvalue(), content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="usage-{account}-{label}.csv"'

        return response


class AdminTariffView(APIView):
    """
    GET /api/v1/admin/tariffs — the price list this account is billed by.
    PUT /api/v1/admin/tariffs — set its rates.

    An administrator edits their OWN account's list. Editing the shared default
    is deliberately not possible here: one account's admin must not be able to
    change what every other account pays.
    """

    permission_classes = [IsAdmin]

    def get(self, request):
        account, _ = account_domain(request)

        own = Tariff.objects.filter(account__iexact=account).first()
        default = Tariff.objects.filter(account='').first()
        effective = own or default

        return Response(
            {
                'data': {
                    'currency': (effective.currency if effective else 'UZS'),
                    'name': effective.name if effective else None,
                    'inherited': own is None,
                    'rates': [
                        {
                            'resource': resource.value,
                            'label': resource.label,
                            'pricePerMonth': float(
                                next(
                                    (
                                        rate.price_per_month
                                        for rate in (effective.rates.all() if effective else [])
                                        if rate.resource == resource.value
                                    ),
                                    0,
                                )
                            ),
                        }
                        for resource in Resource
                    ],
                },
                'meta': {'account': account, 'hasDefault': default is not None},
            }
        )

    def put(self, request):
        account, _ = account_domain(request)

        payload = request.data if isinstance(request.data, dict) else {}
        currency = str(payload.get('currency') or 'UZS').strip()[:8]
        submitted = payload.get('rates')
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
                price = float(value)
            except (TypeError, ValueError):
                raise AppError(message=f'Price for {resource} is not a number', code='invalid_request', status_code=400)

            if price < 0:
                raise AppError(message=f'Price for {resource} is negative', code='invalid_request', status_code=400)

            prices[resource] = price

        tariff, _created = Tariff.objects.update_or_create(
            account=account,
            defaults={'name': f'{account} price list', 'currency': currency, 'is_active': True},
        )

        for resource, price in prices.items():
            TariffRate.objects.update_or_create(
                tariff=tariff, resource=resource, defaults={'price_per_month': price}
            )

        # A resource left out of the payload is cleared rather than kept: an
        # editor that silently preserves an old price is how stale bills happen.
        TariffRate.objects.filter(tariff=tariff).exclude(resource__in=prices).delete()

        return self.get(request)


class AdminUsageHistoryView(APIView):
    """
    GET /api/v1/admin/billing/history?months=6 — the account's monthly totals.

    Recomputed from quantities each time, so correcting a price corrects history
    too.
    """

    permission_classes = [IsAdmin]

    def get(self, request):
        account, domain_id = account_domain(request)

        try:
            months = min(max(int(request.query_params.get('months') or 6), 1), 24)
        except ValueError:
            raise AppError(message='months must be a number', code='invalid_request', status_code=400)

        tariff = rate_engine.resolve_tariff(account)
        rates = rate_engine.rate_map(tariff)

        buckets: dict[str, list] = {}
        for snapshot in UsageSnapshot.objects.filter(domain_id=domain_id).order_by('taken_on'):
            buckets.setdefault(f'{snapshot.taken_on.year:04d}-{snapshot.taken_on.month:02d}', []).append(snapshot)

        history = []
        for period, rows in sorted(buckets.items())[-months:]:
            cost = rate_engine.cost_of(rows, rates)
            history.append({'period': period, 'total': cost['total'], 'days': cost['days']})

        return Response({'data': history, 'meta': {'account': account, 'currency': tariff.currency if tariff else None}})


class AdminInvoiceView(APIView):
    """
    GET  /api/v1/admin/billing/invoice?period=YYYY-MM — the invoice for a month.
    POST /api/v1/admin/billing/invoice {period} — issue it.

    GET returns the issued document if there is one and a live draft otherwise,
    in the same shape, so one screen renders both. Issuing is what freezes the
    figures; before that the draft follows the tariff, which is why it is worth
    looking at before pressing the button.
    """

    permission_classes = [IsAdmin]

    def get(self, request):
        account, domain_id = account_domain(request)
        first, last, label = _period(request.query_params)

        issued = Invoice.objects.filter(account__iexact=account, period=label).first()
        document = invoice_engine.serialize(issued) if issued else invoice_engine.draft(
            account, domain_id, first, last, label
        )

        return Response({'data': document, 'meta': {'account': account, 'period': label}})

    def post(self, request):
        account, domain_id = account_domain(request)
        payload = request.data if isinstance(request.data, dict) else {}

        first, last, label = _period(payload)

        if first > date.today():
            raise AppError(
                message='That month has not started yet.',
                code='invalid_request',
                status_code=400,
            )

        document = invoice_engine.draft(account, domain_id, first, last, label)

        # Refused here, not only in the browser. Issuing consumes a number and
        # freezes the figures, so a document that is not a valid счёт-фактура
        # must never get one — a UI-only check let an empty one through once.
        if document['missingRequisites']:
            raise AppError(
                message='Cannot issue: missing ' + ', '.join(document['missingRequisites']),
                code='requisites_incomplete',
                status_code=409,
            )

        if document['daysMeasured'] == 0:
            raise AppError(
                message='Nothing was measured in that month, so there is nothing to invoice.',
                code='nothing_to_invoice',
                status_code=409,
            )
        if not any(line['priced'] for line in document['lines']):
            raise AppError(
                message='No priced resources in that month — set a price list first.',
                code='tariff_not_configured',
                status_code=409,
            )

        # The readable name, not USERNAME_FIELD — that is the Zadara user id,
        # which means nothing to whoever reads the document later.
        issuer = getattr(request.user, 'username', '') or request.user.get_username()
        issued = invoice_engine.issue(account, domain_id, first, last, label, issuer)
        record(
            request,
            'invoice.issue',
            resource_type='invoice',
            resource_id=issued['number'],
            resource_name=f'{account} {label}',
            outcome=AuditLog.SUCCESS,
            detail={'total': issued['total'], 'currency': issued['currency']},
        )

        return Response({'data': issued, 'meta': {'account': account, 'period': label}})


class AdminInvoiceListView(APIView):
    """GET /api/v1/admin/invoices — the account's issued documents, newest first."""

    permission_classes = [IsAdmin]

    def get(self, request):
        account, _ = account_domain(request)

        invoices = Invoice.objects.filter(account__iexact=account).order_by('-period')

        return Response(
            {
                'data': [
                    {
                        'number': invoice.number,
                        'period': invoice.period,
                        'issuedAt': invoice.issued_at.isoformat(),
                        'issuedBy': invoice.issued_by,
                        'currency': invoice.currency,
                        'subtotal': float(invoice.subtotal),
                        'vatAmount': float(invoice.vat_amount),
                        'total': float(invoice.total),
                    }
                    for invoice in invoices
                ],
                'meta': {'account': account, 'total': invoices.count()},
            }
        )


class AdminBillingProfileView(APIView):
    """
    GET|PUT /api/v1/admin/billing/profile — the buyer's requisites for invoices.

    The seller's side is configuration, not a table: it is us, and it must not be
    editable by an account administrator.
    """

    permission_classes = [IsAdmin]

    FIELDS = ('legal_name', 'tax_id', 'address', 'contract', 'director', 'email', 'phone')

    def _payload(self, account: str) -> dict:
        profile = BillingProfile.objects.filter(account__iexact=account).first()

        return {
            'data': {
                'account': account,
                'legalName': profile.legal_name if profile else '',
                'taxId': profile.tax_id if profile else '',
                'address': profile.address if profile else '',
                'contract': profile.contract if profile else '',
                'director': profile.director if profile else '',
                'email': profile.email if profile else '',
                'phone': profile.phone if profile else '',
            },
            'meta': {'seller': invoice_engine.seller()},
        }

    def get(self, request):
        account, _ = account_domain(request)

        return Response(self._payload(account))

    def put(self, request):
        account, _ = account_domain(request)
        payload = request.data if isinstance(request.data, dict) else {}

        incoming = {
            'legal_name': payload.get('legalName'),
            'tax_id': payload.get('taxId'),
            'address': payload.get('address'),
            'contract': payload.get('contract'),
            'director': payload.get('director'),
            'email': payload.get('email'),
            'phone': payload.get('phone'),
        }
        values = {key: str(value).strip()[:500] for key, value in incoming.items() if value is not None}

        BillingProfile.objects.update_or_create(account=account, defaults=values)

        return Response(self._payload(account))
