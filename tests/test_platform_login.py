"""
Signing in to the operator's panel: password, then a second factor — always.

The second factor is TOTP, or a static access code for an operator an owner
gave one. Asserted through HTTP, because the only question that matters is
whether a session comes out the other end.
"""

import pyotp
import pytest
from django.core.management import CommandError, call_command
from django.utils import timezone

from apps.platform_admin import totp
from apps.platform_admin.models import MAX_FAILED_ATTEMPTS, OWNER, PlatformAdmin

pytestmark = [pytest.mark.urls('config.urls_admin'), pytest.mark.django_db]

EMAIL = 'owner@opencloud.uz'
PASSWORD = 'correct-horse-9!'
CODE = '48151623'

LOGIN = '/api/v1/platform/auth/login'
VERIFY = '/api/v1/platform/auth/login/verify'
ME = '/api/v1/platform/auth/me'


def make_operator(*, code=None, totp_secret=None):
    admin = PlatformAdmin(email=EMAIL, name='Owner', role=OWNER)
    admin.set_password(PASSWORD)
    admin.set_access_code(code)
    if totp_secret:
        admin.totp_secret = totp.encrypt(totp_secret)
        admin.totp_confirmed_at = timezone.now()
    admin.save()
    return admin


def step_one(client):
    response = client.post(LOGIN, {'email': EMAIL, 'password': PASSWORD}, content_type='application/json')
    assert response.status_code == 200
    return response.json()['data']


def step_two(client, ticket, code):
    return client.post(VERIFY, {'ticket': ticket, 'code': code}, content_type='application/json')


def signed_in_as(client):
    response = client.get(ME)
    return response.json()['data']['email'] if response.status_code == 200 else None


# --- the static access code ----------------------------------------------------


def test_an_operator_with_an_access_code_signs_in_with_it(client):
    make_operator(code=CODE)

    challenge = step_one(client)
    assert challenge['stage'] == 'code'
    # Nothing to enrol, so no TOTP secret travels to the browser.
    assert 'secret' not in challenge and 'otpauthUri' not in challenge

    assert step_two(client, challenge['ticket'], CODE).status_code == 200
    assert signed_in_as(client) == EMAIL


def test_a_wrong_access_code_opens_nothing(client):
    make_operator(code=CODE)

    response = step_two(client, step_one(client)['ticket'], '00000000')

    assert response.status_code == 401
    assert response.json()['error']['code'] == 'invalid_access_code'
    assert signed_in_as(client) is None


def test_the_lockout_holds_at_the_second_step(client):
    """The hole this closes: the ticket outlived the lockout, so a known password
    bought four minutes of unlimited guesses at the code."""
    admin = make_operator(code=CODE)
    ticket = step_one(client)['ticket']

    for _ in range(MAX_FAILED_ATTEMPTS):
        assert step_two(client, ticket, '00000000').status_code == 401

    response = step_two(client, ticket, CODE)  # the right code, too late
    assert response.status_code == 403
    assert response.json()['error']['code'] == 'account_locked'
    assert signed_in_as(client) is None

    # And the ticket is gone, not merely paused until the lock lifts.
    PlatformAdmin.objects.filter(pk=admin.pk).update(locked_until=None)
    assert step_two(client, ticket, CODE).json()['error']['code'] == 'mfa_expired'


def test_the_access_code_is_not_stored_in_the_clear():
    admin = make_operator(code=CODE)

    assert admin.access_code_hash and CODE not in admin.access_code_hash
    assert admin.check_access_code(CODE)


def test_the_code_replaces_totp_rather_than_joining_it(client):
    """Either-or would let the weaker factor stand in for the stronger one."""
    secret = pyotp.random_base32()
    make_operator(code=CODE, totp_secret=secret)

    challenge = step_one(client)
    assert challenge['stage'] == 'code'

    response = step_two(client, challenge['ticket'], pyotp.TOTP(secret).now())
    assert response.status_code == 401
    assert signed_in_as(client) is None


def test_clearing_the_code_returns_the_operator_to_their_authenticator(client):
    secret = pyotp.random_base32()
    admin = make_operator(code=CODE, totp_secret=secret)

    admin.set_access_code(None)
    admin.save()

    challenge = step_one(client)
    assert challenge['stage'] == 'totp'
    assert step_two(client, challenge['ticket'], CODE).status_code == 401
    assert step_two(client, challenge['ticket'], pyotp.TOTP(secret).now()).status_code == 200


# --- TOTP stays the default for everyone else ---------------------------------


def test_an_operator_without_a_code_enrols_totp_on_first_sign_in(client):
    admin = make_operator()

    challenge = step_one(client)
    assert challenge['stage'] == 'enroll'

    assert step_two(client, challenge['ticket'], pyotp.TOTP(challenge['secret']).now()).status_code == 200
    assert signed_in_as(client) == EMAIL

    admin.refresh_from_db()
    assert admin.totp_confirmed_at is not None


def test_an_enrolled_operator_signs_in_with_totp(client):
    secret = pyotp.random_base32()
    make_operator(totp_secret=secret)

    challenge = step_one(client)
    assert challenge['stage'] == 'totp'

    response = step_two(client, challenge['ticket'], '000000')
    assert response.json()['error']['code'] == 'invalid_mfa_code'

    assert step_two(client, challenge['ticket'], pyotp.TOTP(secret).now()).status_code == 200
    assert signed_in_as(client) == EMAIL


# --- the command that sets it --------------------------------------------------


def _typing(monkeypatch, value):
    monkeypatch.setattr('apps.platform_admin.management.prompts.getpass', lambda prompt='': value)


def test_the_command_sets_and_clears_the_code(monkeypatch):
    admin = make_operator()

    _typing(monkeypatch, CODE)
    call_command('setplatformadmincode', '--email', EMAIL)
    admin.refresh_from_db()
    assert admin.check_access_code(CODE)

    call_command('setplatformadmincode', '--email', EMAIL, '--clear')
    admin.refresh_from_db()
    assert not admin.uses_access_code


@pytest.mark.parametrize(
    ('code', 'why'),
    [('12345', 'at least 6'), (PASSWORD, 'differ from the password')],
)
def test_the_command_refuses_a_weak_code(monkeypatch, code, why):
    make_operator()
    _typing(monkeypatch, code)

    with pytest.raises(CommandError, match=why):
        call_command('setplatformadmincode', '--email', EMAIL)

    assert not PlatformAdmin.objects.get(email=EMAIL).uses_access_code
