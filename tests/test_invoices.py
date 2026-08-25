"""
Issuing an invoice.

The document is the one place money is stored rather than recomputed, so the
tests here are about the consequences of that: it must freeze, it must not be
issuable while it would be invalid, and a number must never be reused.
"""

from datetime import date, datetime, timezone

import pytest

from apps.billing import invoices as engine
from apps.billing.models import BillingProfile, Invoice, Tariff, TariffRate, UsageSnapshot

SELLER = {
    'name': 'OOO Seller',
    'taxId': '300000000',
    'address': 'Tashkent',
    'bank': 'Bank',
    'bankAccount': '20208000000000000000',
    'bankCode': '00014',
    'director': 'Director',
    'accountant': 'Accountant',
    'phone': '',
    'email': '',
}


@pytest.fixture
def priced(db, settings):
    settings.BILLING_SELLER = dict(SELLER)
    settings.BILLING_VAT_RATE = '0'

    tariff = Tariff.objects.create(name='Default', currency='UZS', account='', is_active=True)
    TariffRate.objects.create(tariff=tariff, resource='vcpu', price_per_month=46000)
    TariffRate.objects.create(tariff=tariff, resource='ssd_gb', price_per_month=1900)

    BillingProfile.objects.create(account='Acme', legal_name='OOO Acme', tax_id='301234567')

    for day in range(1, 32):
        UsageSnapshot.objects.create(
            taken_on=date(2026, 1, day),
            taken_at=datetime(2026, 1, day, tzinfo=timezone.utc),
            account='Acme',
            domain_id='dom-acme',
            project_id='p-1',
            project_name='Alpha',
            vcpus=2,
            ssd_gib=50,
            vms_total=1,
            vms_running=1,
        )

    return tariff


JAN = (date(2026, 1, 1), date(2026, 1, 31))


@pytest.mark.django_db
def test_draft_tracks_the_tariff_but_an_issued_document_does_not(priced):
    """The whole point of freezing: a price correction must not rewrite a sent bill."""
    before = engine.draft('Acme', 'dom-acme', *JAN, '2026-01')
    issued = engine.issue('Acme', 'dom-acme', *JAN, '2026-01', 'tester')

    assert issued['total'] == before['total']

    TariffRate.objects.filter(resource='vcpu').update(price_per_month=99999)

    after = engine.draft('Acme', 'dom-acme', *JAN, '2026-02')  # a different month, so still a draft
    reread = engine.serialize(Invoice.objects.get(number=issued['number']))

    assert reread['total'] == issued['total'], 'an issued invoice must not move when prices change'
    assert after['total'] != issued['total'] or after['daysMeasured'] == 0


@pytest.mark.django_db
def test_issuing_twice_returns_the_same_document(priced):
    first = engine.issue('Acme', 'dom-acme', *JAN, '2026-01', 'tester')
    second = engine.issue('Acme', 'dom-acme', *JAN, '2026-01', 'tester')

    assert first['number'] == second['number']
    assert Invoice.objects.count() == 1, 'one document per account per month'
    assert first['issuedAt'] == second['issuedAt'], 'the same document, not a fresh one wearing its number'


@pytest.mark.django_db
def test_reissuing_after_a_price_change_returns_the_original_figures(priced):
    """A second `issue` must hand back the frozen document, not quietly rebuild it.

    Written after a mutation test slipped through: deleting and recreating the
    invoice kept both the number and the row count, so the earlier assertions
    could not tell the difference. The amount is what proves identity.
    """
    original = engine.issue('Acme', 'dom-acme', *JAN, '2026-01', 'tester')

    TariffRate.objects.filter(resource='vcpu').update(price_per_month=99999)

    again = engine.issue('Acme', 'dom-acme', *JAN, '2026-01', 'tester')

    assert again['total'] == original['total'], 'a re-issue must not re-price a sent document'
    assert again['issuedAt'] == original['issuedAt']
    assert Invoice.objects.count() == 1


@pytest.mark.django_db
def test_numbers_are_sequential_within_the_year(priced):
    BillingProfile.objects.create(account='Beta', legal_name='OOO Beta', tax_id='309999999')
    for day in range(1, 32):
        UsageSnapshot.objects.create(
            taken_on=date(2026, 1, day),
            taken_at=datetime(2026, 1, day, tzinfo=timezone.utc),
            account='Beta',
            domain_id='dom-beta',
            project_id='p-2',
            vcpus=1,
            vms_total=1,
            vms_running=1,
        )

    first = engine.issue('Acme', 'dom-acme', *JAN, '2026-01', 'tester')
    second = engine.issue('Beta', 'dom-beta', *JAN, '2026-01', 'tester')

    assert first['number'] == 'СФ-2026-0001'
    assert second['number'] == 'СФ-2026-0002'
    assert first['number'] != second['number']


@pytest.mark.django_db
def test_requisites_are_reported_missing_rather_than_guessed(priced, settings):
    settings.BILLING_SELLER = dict(SELLER, taxId='', bankAccount='')
    BillingProfile.objects.filter(account='Acme').update(tax_id='')

    document = engine.draft('Acme', 'dom-acme', *JAN, '2026-01')

    assert 'seller ИНН' in document['missingRequisites']
    assert 'seller bank account' in document['missingRequisites']
    assert 'buyer ИНН' in document['missingRequisites']


@pytest.mark.django_db
def test_a_complete_document_reports_nothing_missing(priced):
    assert engine.draft('Acme', 'dom-acme', *JAN, '2026-01')['missingRequisites'] == []


@pytest.mark.django_db
def test_both_parties_are_frozen_into_the_document(priced):
    issued = engine.issue('Acme', 'dom-acme', *JAN, '2026-01', 'tester')

    BillingProfile.objects.filter(account='Acme').update(legal_name='Renamed Later', address='Somewhere else')

    reread = engine.serialize(Invoice.objects.get(number=issued['number']))

    assert reread['buyer']['name'] == 'OOO Acme', 'a later rename must not alter a sent document'
    assert reread['seller']['taxId'] == SELLER['taxId']


@pytest.mark.django_db
def test_unpriced_lines_are_left_off_the_issued_document(priced):
    """Shown on the draft with a warning; charging 0 for them would look deliberate."""
    UsageSnapshot.objects.filter(taken_on=date(2026, 1, 1)).update(hdd_gib=10)

    draft = engine.draft('Acme', 'dom-acme', *JAN, '2026-01')
    assert any(line['resource'] == 'hdd_gb' and not line['priced'] for line in draft['lines'])

    issued = engine.issue('Acme', 'dom-acme', *JAN, '2026-01', 'tester')
    assert all(line['resource'] != 'hdd_gb' for line in issued['lines'])


@pytest.mark.django_db
def test_document_rows_multiply_out_and_sum_to_the_subtotal(priced):
    document = engine.draft('Acme', 'dom-acme', *JAN, '2026-01')

    for line in document['lines']:
        assert line['amount'] == pytest.approx(line['quantity'] * line['unitPrice']), line['resource']

    priced_rows = sum(line['amount'] for line in document['lines'] if line['priced'])
    assert document['subtotal'] == pytest.approx(priced_rows)


@pytest.mark.django_db
def test_month_bounds_covers_the_whole_month():
    assert engine.month_bounds('2026-02') == (date(2026, 2, 1), date(2026, 2, 28))
    assert engine.month_bounds('2024-02') == (date(2024, 2, 1), date(2024, 2, 29))
    assert engine.month_bounds('2026-12') == (date(2026, 12, 1), date(2026, 12, 31))
