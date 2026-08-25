"""
What happens when the cloud token dies.

A Zadara token lives about four hours and cannot be refreshed without the
password, so every session here ends. It used to end badly: the Django session
cookie was good for two weeks, `/auth/me` needs no cloud token, so the guards
let the whole console render — and then every panel on it failed. One view even
answered 502, which reads as "the cloud is broken" rather than "sign in again".

These tests pin the three things that make an expiry legible: the session cannot
outlive the token, a dead token is 401 `session_expired` wherever it surfaces,
and a wrong password is still a wrong password.
"""

import pytest
from django.conf import settings

from apps.integrations.zadara import resources
from apps.integrations.zadara.exceptions import ZadaraError


class FakeResponse:
    """Just enough of requests.Response for the client to react to a status."""

    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.headers = {}
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError('no body')

        return self._payload


# --- the session must not outlive the token it depends on ------------------


def test_the_session_cookie_expires_with_the_vaulted_token():
    """Two weeks of session over three hours of token is the whole bug."""
    assert settings.SESSION_COOKIE_AGE == settings.ZADARA_TOKEN_TTL


def test_the_vault_ttl_comes_from_the_same_setting():
    from apps.authentication import vault

    assert vault.ttl_seconds() == settings.ZADARA_TOKEN_TTL


# --- a dead token is 401, whichever view catches it ------------------------


@pytest.mark.django_db
def test_a_rejected_token_answers_401_session_expired(client, signed_in, monkeypatch):
    """Through HTTP, on the view that used to answer 502 for exactly this."""
    signed_in()
    monkeypatch.setattr(resources, 'request', lambda *a, **kw: FakeResponse(401))

    response = client.get('/api/v1/user/vms')

    assert response.status_code == 401, 'the browser cannot tell that 502 means "sign in again"'
    assert response.json()['error']['code'] == 'session_expired'


@pytest.mark.django_db
def test_the_client_maps_an_upstream_401_to_session_expired(monkeypatch):
    """The mapping itself, one layer down: 401 from the cloud is a dead session."""
    monkeypatch.setattr(resources, 'request', lambda *a, **kw: FakeResponse(401))

    with pytest.raises(ZadaraError) as raised:
        resources._authed_get('/api/v2/vms', 'stale-token')

    assert raised.value.code == 'session_expired'


@pytest.mark.django_db
def test_an_empty_vault_answers_401_before_reaching_the_cloud(client, make_user):
    """The token is gone from the cache but the cookie is still there."""
    user = make_user(username='bob', account='Acme')
    client.force_login(user)

    response = client.get('/api/v1/user/vms')

    assert response.status_code == 401
    assert response.json()['error']['code'] == 'session_expired'


# --- and a wrong password is still a wrong password ------------------------


@pytest.mark.django_db
def test_a_wrong_password_is_not_reported_as_an_expired_session(client, monkeypatch):
    """The sign-in form must keep saying what it always said."""
    from apps.integrations.zadara import auth as zadara_auth

    def refuse(*args, **kwargs):
        raise ZadaraError('invalid_credentials', 'Invalid account, username or password', 401)

    monkeypatch.setattr(zadara_auth, 'authenticate', refuse)

    response = client.post(
        '/api/v1/auth/login',
        {'account': 'Acme', 'username': 'alice', 'password': 'wrong'},
        content_type='application/json',
    )

    assert response.status_code == 401
    assert response.json()['error']['code'] == 'invalid_credentials'
