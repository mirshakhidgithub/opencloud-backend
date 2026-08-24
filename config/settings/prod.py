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
