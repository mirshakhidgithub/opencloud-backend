"""
The response cache and the concurrent fetcher.

Both were added for speed, and both can fail in ways that are worse than being
slow: a cache keyed carelessly serves one tenant another tenant's machines, and
a fetcher that lets one refusal escape turns a partial page into no page.
"""

import threading
import time

import pytest

from apps.common.concurrency import gather
from apps.integrations.zadara import resources


# --- the cache must never mix tenants -------------------------------------


@pytest.fixture
def counting_fetch(monkeypatch):
    """Replaces the network with a counter, so hits and misses are observable."""
    calls = []

    def fake(path, token):
        calls.append((path, token))

        return {'path': path, 'token': token}

    monkeypatch.setattr(resources, '_authed_get', fake)

    return calls


def test_two_tokens_never_share_a_cache_entry(counting_fetch):
    """The failure this prevents is one customer seeing another's resources."""
    first = resources._cached_get('/api/v2/vms', 'token-tenant-a')
    second = resources._cached_get('/api/v2/vms', 'token-tenant-b')

    assert first['token'] == 'token-tenant-a'
    assert second['token'] == 'token-tenant-b'
    assert len(counting_fetch) == 2, 'the same path with a different token must be fetched again'


def test_the_same_token_is_served_from_cache(counting_fetch):
    resources._cached_get('/api/v2/vms', 'token-a')
    resources._cached_get('/api/v2/vms', 'token-a')

    assert len(counting_fetch) == 1


def test_different_paths_are_cached_separately(counting_fetch):
    resources._cached_get('/api/v2/vms', 'token-a')
    resources._cached_get('/api/v3/volumes', 'token-a')

    assert len(counting_fetch) == 2


def test_the_token_is_not_recoverable_from_the_key():
    scope = resources._scope('super-secret-token-value')

    assert 'super-secret-token-value' not in scope
    assert len(scope) == 16


def test_invalidate_clears_only_its_own_scope(counting_fetch):
    resources._cached_get('/api/v2/vms', 'token-a')
    resources._cached_get('/api/v2/vms', 'token-b')
    assert len(counting_fetch) == 2

    resources.invalidate('token-a')

    resources._cached_get('/api/v2/vms', 'token-a')
    assert len(counting_fetch) == 3, 'the invalidated scope refetches'

    resources._cached_get('/api/v2/vms', 'token-b')
    assert len(counting_fetch) == 3, 'a neighbour’s cache must survive someone else’s write'


def test_a_none_response_is_not_cached(monkeypatch):
    """`None` is a real answer from a body-less reply; caching it looks like a miss."""
    calls = []

    def fake(path, token):
        calls.append(path)

        return None

    monkeypatch.setattr(resources, '_authed_get', fake)

    resources._cached_get('/api/v2/thing', 'token-a')
    resources._cached_get('/api/v2/thing', 'token-a')

    assert len(calls) == 2


# --- gather ---------------------------------------------------------------


def test_one_failure_does_not_take_the_others_with_it():
    def boom():
        raise ValueError('refused')

    results = gather({'good': lambda: 'value', 'bad': boom})

    assert results['good'].ok and results['good'].value == 'value'
    assert not results['bad'].ok
    assert isinstance(results['bad'].error, ValueError)


def test_gather_never_raises():
    results = gather({'a': lambda: 1 / 0, 'b': lambda: 1 / 0})

    assert all(not r.ok for r in results.values())


def test_sources_really_run_at_the_same_time():
    """Otherwise the whole point of the helper is lost without anything failing."""

    def slow():
        time.sleep(0.2)

        return 'done'

    start = time.time()
    results = gather({f'source-{i}': slow for i in range(4)})
    elapsed = time.time() - start

    assert all(r.ok for r in results.values())
    assert elapsed < 0.5, f'four 0.2s sources took {elapsed:.2f}s — they ran in sequence'


def test_a_single_source_runs_inline():
    """No pool for one task, and it must still behave identically."""
    where = {}

    def note():
        where['thread'] = threading.current_thread().name

        return 'value'

    results = gather({'only': note})

    assert results['only'].value == 'value'
    assert where['thread'] == threading.current_thread().name


def test_empty_input_is_not_an_error():
    assert gather({}) == {}


def test_results_are_keyed_by_name_not_by_order():
    results = gather({'first': lambda: 1, 'second': lambda: 2, 'third': lambda: 3})

    assert {name: r.value for name, r in results.items()} == {'first': 1, 'second': 2, 'third': 3}
