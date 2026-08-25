"""Production settings. Secrets come from the environment / secrets manager."""

from .base import *  # noqa: F401,F403
from .base import env

DEBUG = False

# Enforce HTTPS-only, secure cookies (spec §9.1).
# SSL redirect is env-toggleable so local docker (no TLS) can run over http.
SESSION_COOKIE_SECURE = env('SESSION_COOKIE_SECURE', default=True)
CSRF_COOKIE_SECURE = env('CSRF_COOKIE_SECURE', default=True)
SECURE_SSL_REDIRECT = env('SECURE_SSL_REDIRECT', default=True)
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SECURE_CONTENT_TYPE_NOSNIFF = True

# --------------------------------------------------------------------------- #
# Static files
# --------------------------------------------------------------------------- #
# WhiteNoise serves Django's own static files (admin, Swagger UI) from gunicorn,
# so nginx needs no shared volume with the container. It belongs directly after
# SecurityMiddleware, and only in production: dev and tests have no collected
# staticfiles directory, and it warns about the missing one on every request.
_security = MIDDLEWARE.index('django.middleware.security.SecurityMiddleware')
MIDDLEWARE = (
    MIDDLEWARE[: _security + 1]
    + ['whitenoise.middleware.WhiteNoiseMiddleware']
    + MIDDLEWARE[_security + 1 :]
)


# Hashed filenames + pre-compressed copies, served by WhiteNoise. `collectstatic`
# runs in the image build, so the manifest always exists by the time gunicorn
# starts.
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage'},
}

# --------------------------------------------------------------------------- #
# Refuse to start on a misconfiguration that would "work" while being wrong
# --------------------------------------------------------------------------- #
# Each of these fails silently rather than loudly if it is left at its default,
# which is exactly why they are checked here: a weak key or a per-process cache
# looks healthy in the logs and costs users their session (or their secrecy).
_errors = []

if SECRET_KEY in ('', 'dev-insecure-change-me') or len(SECRET_KEY) < 50:
    _errors.append(
        'DJANGO_SECRET_KEY is unset, still the development default, or too short. '
        'Generate one with: python -c "import secrets; print(secrets.token_urlsafe(64))"'
    )

if not env('TOKEN_VAULT_KEY', default=''):
    _errors.append(
        'TOKEN_VAULT_KEY is unset. The vault would fall back to a key derived from '
        'SECRET_KEY, so rotating the secret would silently sign every user out. '
        'Generate one with: python -c "from cryptography.fernet import Fernet; '
        'print(Fernet.generate_key().decode())"'
    )

if not env.bool('USE_REDIS_CACHE', default=False):
    _errors.append(
        'USE_REDIS_CACHE is false. The Zadara token vault and MFA tickets live in '
        'the cache; with a per-process LocMemCache each gunicorn worker would hold '
        'a different one and users would be logged out at random.'
    )

if DATABASES['default']['ENGINE'].endswith('sqlite3'):
    _errors.append('DATABASE_URL is unset — production would run on sqlite.')

if _errors:
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured('Production settings are incomplete:\n  - ' + '\n  - '.join(_errors))
