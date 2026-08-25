"""
The response cache and the concurrent fetcher.

Both were added for speed, and both can fail in ways that are worse than being
slow: a cache keyed carelessly serves one tenant another tenant's machines, and
a fetcher that lets one refusal escape turns a partial page into no page.
"""

import threading
import time

import pytest

from apps.common import concurrency
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


# --- the service-mode directory -------------------------------------------


@pytest.fixture
def counting_svc(monkeypatch):
    from apps.integrations.zadara import service

    calls = []

    def fake(path):
        calls.append(path)

        return [{'id': 'dom-1', 'name': 'Acme'}]

    monkeypatch.setattr(service, '_svc_get', fake)

    return calls


def test_the_account_directory_is_cached(counting_svc):
    from apps.integrations.zadara import service

    service.list_domains()
    service.list_domains()

    assert len(counting_svc) == 1, 'accounts change when an operator adds one, not per request'


def test_user_lists_are_never_cached(counting_svc):
    """An administrator who has just created someone must see them immediately.

    Those writes go out with the admin's own token, so this scope would never
    learn to expire — the only safe answer is not to cache it at all.
    """
    from apps.integrations.zadara import service

    service.list_domain_users('dom-1')
    service.list_domain_users('dom-1')

    assert len(counting_svc) == 2


def test_project_lists_are_cached_per_domain(counting_svc):
    from apps.integrations.zadara import service

    service.list_domain_projects('dom-1')
    service.list_domain_projects('dom-1')
    service.list_domain_projects('dom-2')

    assert len(counting_svc) == 2, 'one fetch per domain, then served from cache'


# --- a single source must not cost the caller its database connection ------
#
# Asserted on the call itself rather than by watching a connection die, because
# the test database is in-memory sqlite and its `close()` is deliberately a
# no-op — the real consequence (a request whose connection is pulled away, and
# inside a transaction an atomic block poisoned for every later query) cannot be
# reproduced on this backend. What is testable is the decision: close in pool
# threads, never on the thread that called us.


class RecordingConnections:
    """Stands in for django.db.connections, remembering who closed what."""

    def __init__(self):
        self.closed_by = []

    def close_all(self):
        self.closed_by.append(threading.current_thread().name)


def test_the_inline_path_does_not_close_the_caller_s_connections(monkeypatch):
    """One source runs inline, on the REQUEST thread. Closing there throws away
    the connection the request is using."""
    recorder = RecordingConnections()
    monkeypatch.setattr(concurrency, 'connections', recorder)

    result = gather({'only': lambda: 'value'})

    assert result['only'].value == 'value'
    assert recorder.closed_by == [], 'the caller\'s own connection must survive its own gather'


def test_worker_threads_close_their_own_connections(monkeypatch):
    """The reason the close exists: Django connections are thread-local and are
    tidied up only on the request thread, so a pool thread that touched the ORM
    leaks one — and those accumulate until the database refuses new ones."""
    recorder = RecordingConnections()
    monkeypatch.setattr(concurrency, 'connections', recorder)
    here = threading.current_thread().name

    results = gather({f'source-{i}': (lambda i=i: i) for i in range(3)})

    assert sorted(r.value for r in results.values()) == [0, 1, 2]
    assert len(recorder.closed_by) == 3, 'every worker must clean up after itself'
    assert here not in recorder.closed_by


def test_a_failing_source_still_closes_its_thread(monkeypatch):
    """The leak would be worst on the error path, which is where it is easiest
    to forget."""
    recorder = RecordingConnections()
    monkeypatch.setattr(concurrency, 'connections', recorder)

    def boom():
        raise RuntimeError('refused')

    results = gather({'ok': lambda: 1, 'boom': boom})

    assert results['ok'].ok and not results['boom'].ok
    assert len(recorder.closed_by) == 2
