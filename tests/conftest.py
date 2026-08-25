"""
Shared fixtures.

Everything that would otherwise reach Zadara is patched at the client boundary
(`apps.integrations.zadara.*`), never at the `requests` level: patching lower
would let a change in our own client slip through a green suite.
"""

import pytest
from django.core.cache import cache

from apps.accounts.models import User
from apps.accounts.roles import ADMIN


@pytest.fixture(autouse=True)
def clean_cache():
    """The response cache is process-wide; a leak between tests would hide bugs."""
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def make_user(db):
    def _make(username='alice', account='Acme', role=ADMIN, **extra):
        return User.objects.create_user(
            zadara_user_id=f'zid-{username}',
            username=username,
            account=account,
            app_role=role,
            **extra,
        )

    return _make


@pytest.fixture
def signed_in(client, make_user, monkeypatch):
    """An authenticated request client, with a token in the vault.

    Sessions are DB-backed and the vault is the cache, so both have to be set up
    the way the login view would have done it.
    """
    from apps.authentication import vault

    def _sign_in(account='Acme', role=ADMIN, project_id='proj-1', project_name='Main', token='tok-acme'):
        user = make_user(username=f'user-{account}', account=account, role=role)
        client.force_login(user)

        session = client.session
        session['zadara_project_id'] = project_id
        session['zadara_project_name'] = project_name
        session.save()

        vault.store(session.session_key, token)

        return user

    return _sign_in
