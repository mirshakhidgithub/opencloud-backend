"""
Settings for the test suite.

Deliberately hermetic: an in-memory database and an in-memory cache, so a run
touches neither the developer's sqlite file nor the Redis that holds real
sessions. Nothing here reaches the cloud — every test that needs Zadara data
patches the client, because a suite that depends on a live cluster tells you
about the cluster, not about your code.
"""

from .base import *  # noqa: F403

DEBUG = False

DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}}

CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}

# A fixed key so vault/MFA encryption is deterministic across runs.
SECRET_KEY = 'test-only-not-a-secret'
TOKEN_VAULT_KEY = None

ZADARA_API_URL = 'https://cloud.test'
ZADARA_SERVICE_ACCOUNT = 'test_msp'
ZADARA_SERVICE_USERNAME = 'test_svc'
ZADARA_SERVICE_PASSWORD = 'test-password'
ZADARA_SERVICE_PROJECT = 'Test Project'

# Money maths is verified against explicit numbers, so pin the tax settings
# rather than inheriting whatever the developer's .env happens to say.
BILLING_VAT_RATE = '0'
BILLING_PRICES_INCLUDE_VAT = False

PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
