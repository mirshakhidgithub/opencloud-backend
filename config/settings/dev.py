"""Development settings."""

from .base import *  # noqa: F401,F403
from .base import env

DEBUG = env('DJANGO_DEBUG', default=True)
ALLOWED_HOSTS = ['*']

# Relaxed cookies for local http development.
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
