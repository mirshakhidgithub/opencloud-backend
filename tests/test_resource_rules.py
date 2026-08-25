"""
Rules in the Zadara client that are easy to lose in a refactor.

Each one here was a decision with a reason, and each would fail silently: a
guessed medium bills the wrong price, and a missing ceiling read as zero draws a
full progress bar on an unlimited resource.
"""

import pytest

from apps.integrations.zadara import resources


# --- SSD / HDD -------------------------------------------------------------

TYPES = [
    # Named plainly — the medium is in the name.
    {'id': 't-ssd', 'name': 'uz_serv_01_EBS_SSD_1', 'storage_class_id': 'class-flash', 'is_available': True},
    {'id': 't-hdd', 'name': 'uz_serv_01_EBS_HDD_01', 'storage_class_id': 'class-spin', 'is_available': True},
    # Named by description only.
    {'id': 't-desc', 'name': 'zvthdd3', 'description': 'Zadara HDD Basic 1', 'storage_class_id': 'class-spin2'},
    # Says nothing at all, but shares a storage class with a type that does.
    {'id': 't-mute', 'name': 'zvtx9', 'storage_class_id': 'class-flash'},
    # Says nothing and has no informative sibling.
    {'id': 't-orphan', 'name': 'zvtq1', 'storage_class_id': 'class-unknown'},
]


@pytest.fixture
def classified():
    return resources._classify_volume_types(TYPES)


def test_medium_is_read_from_the_name(classified):
    assert classified['t-ssd']['media'] == 'SSD'
    assert classified['t-hdd']['media'] == 'HDD'


def test_medium_is_read_from_the_description_when_the_name_is_opaque(classified):
    assert classified['t-desc']['media'] == 'HDD'


def test_a_silent_type_inherits_from_its_storage_class(classified):
    """The four classes on this cluster are each wholly one medium."""
    assert classified['t-mute']['media'] == 'SSD'


def test_an_unknowable_type_is_left_blank_rather_than_guessed(classified):
    """An unlabelled disk is honest; a mislabelled one is billed at the wrong price."""
    assert classified['t-orphan']['media'] == ''


def test_a_contradictory_name_is_left_blank():
    both = resources._classify_volume_types([{'id': 'x', 'name': 'ssd-backed-hdd-cache', 'storage_class_id': 'c'}])

    assert both['x']['media'] == ''


def test_volumes_are_labelled_from_their_type(classified):
    volumes = [
        {'volumeTypeId': 't-ssd', 'sizeGiB': 100},
        {'volumeTypeId': 't-hdd', 'sizeGiB': 200},
        {'volumeTypeId': 'gone', 'sizeGiB': 5},
    ]

    resources.label_volume_media(volumes, classified)

    assert [v['media'] for v in volumes] == ['SSD', 'HDD', '']
    assert volumes[0]['volumeType'] == 'uz_serv_01_EBS_SSD_1'


def test_media_totals_keeps_unlabelled_capacity_visible():
    volumes = [
        {'media': 'SSD', 'sizeGiB': 100},
        {'media': 'SSD', 'sizeGiB': 50},
        {'media': 'HDD', 'sizeGiB': 300},
        {'media': '', 'sizeGiB': 7},
    ]

    totals = {row['media']: row for row in resources.media_totals(volumes)}

    assert totals['SSD']['totalGiB'] == 150 and totals['SSD']['volumes'] == 2
    assert totals['HDD']['totalGiB'] == 300
    assert totals['']['totalGiB'] == 7, 'unlabelled capacity must not be folded into a medium'
    assert resources.media_totals(volumes)[-1]['media'] == '', 'and it sorts last'


# --- quotas ----------------------------------------------------------------


def test_no_ceiling_means_unlimited_not_zero():
    """`total: null` is "no limit configured". Read as 0 it would show 100% full."""
    row = resources.normalize_quota({'name': 'cores', 'allocated': 20, 'total': None, 'domain': 'compute'})

    assert row['unlimited'] is True
    assert row['limit'] is None
    assert row['usedPercent'] is None


def test_a_real_ceiling_gives_a_percentage():
    row = resources.normalize_quota({'name': 'cores', 'allocated': 80, 'total': 128, 'domain': 'compute'})

    assert row['unlimited'] is False
    assert row['limit'] == 128
    assert row['usedPercent'] == pytest.approx(62.5)


def test_a_zero_ceiling_is_a_limit_not_an_absence():
    row = resources.normalize_quota({'name': 'x', 'allocated': 0, 'total': 0, 'domain': 'compute'})

    assert row['unlimited'] is False
    assert row['usedPercent'] is None, 'nothing allowed and nothing used — a percentage would be meaningless'


def test_storage_rows_carry_the_volume_type_that_tells_ssd_from_hdd():
    row = resources.normalize_quota(
        {
            'name': 'volumes_abc',
            'allocated': 4,
            'total': None,
            'domain': 'storage',
            'dependencies': [{'type': 'volume_type', 'id': 'abc'}],
        }
    )

    assert row['volumeTypeId'] == 'abc'


def test_an_aggregate_row_has_no_volume_type():
    row = resources.normalize_quota({'name': 'snapshots', 'allocated': 34, 'total': None, 'domain': 'storage'})

    assert row['volumeTypeId'] is None


# --- events ----------------------------------------------------------------


def test_an_unknown_period_is_refused_before_the_cloud_is_called(monkeypatch):
    monkeypatch.setattr(
        resources, '_authed_get', lambda *a, **k: pytest.fail('the cloud must not be called with a bad period')
    )

    with pytest.raises(Exception) as caught:
        resources.list_events('token', project_id='p-1', period='99y')

    assert 'period' in str(caught.value).lower()


def test_an_unknown_severity_is_refused(monkeypatch):
    monkeypatch.setattr(resources, '_authed_get', lambda *a, **k: pytest.fail('must not be called'))

    with pytest.raises(Exception) as caught:
        resources.list_events('token', project_id='p-1', severity='LOUD')

    assert 'severity' in str(caught.value).lower()


def test_events_are_always_pinned_to_a_project(monkeypatch):
    """Unfiltered, a wide token sees the whole cluster's stream."""
    seen = {}

    def fake(path, token):
        seen['path'] = path

        return []

    monkeypatch.setattr(resources, '_authed_get', fake)

    resources.list_events('token', project_id='p-42', period='24h')

    assert 'project_id=p-42' in seen['path']
