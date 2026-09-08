"""
Shared fixtures.

Everything that would otherwise reach Zadara is patched at the client boundary
(`apps.integrations.zadara.*`), never at the `requests` level: patching lower
would let a change in our own client slip through a green suite.
"""

import pytest
from django.conf import settings
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


@pytest.fixture
def platform_client(client, db):
    """A signed-in platform operator, on the admin process's URL map.

    The panel runs as a second Django process with `config.urls_admin`, where
    none of the cabinet's routes exist — so a test that reached the platform
    endpoints through the cabinet's URLconf would be testing a deployment we do
    not run. `pytest.mark.urls` on the test class switches the map; this fixture
    only puts an operator in the session, the way `start_session` does.
    """
    from apps.platform_admin.authentication import SESSION_KEY
    from apps.platform_admin.models import FINANCE, PlatformAdmin

    def _sign_in(role=FINANCE, email='finance@opencloud.uz'):
        admin = PlatformAdmin.objects.create(email=email, name='Operator', role=role)
        admin.set_password('irrelevant-in-tests')
        admin.save()

        session = client.session
        session[SESSION_KEY] = admin.pk
        session.save()
        client.cookies[settings.SESSION_COOKIE_NAME] = session.session_key

        return admin

    return _sign_in
