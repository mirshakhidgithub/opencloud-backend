"""
Base Django settings for the OpenCloud backend.

Configuration is read from the environment (django-environ). See .env.example.
"""

from pathlib import Path

import environ
from celery.schedules import crontab

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env(
    DJANGO_DEBUG=(bool, False),
    DJANGO_ALLOWED_HOSTS=(list, ['localhost', '127.0.0.1']),
    CORS_ALLOWED_ORIGINS=(list, ['http://localhost:3000']),
    CSRF_TRUSTED_ORIGINS=(list, ['http://localhost:3000', 'http://127.0.0.1:3000']),
)

# Read .env if present (local dev). In prod, use real env / secrets manager.
env_file = BASE_DIR / '.env'
if env_file.exists():
    env.read_env(str(env_file))

SECRET_KEY = env('DJANGO_SECRET_KEY', default='dev-insecure-change-me')
DEBUG = env('DJANGO_DEBUG')
ALLOWED_HOSTS = env('DJANGO_ALLOWED_HOSTS')

# --------------------------------------------------------------------------- #
# Applications
# --------------------------------------------------------------------------- #
DJANGO_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
]

THIRD_PARTY_APPS = [
    'rest_framework',
    'corsheaders',
    'drf_spectacular',
]

LOCAL_APPS = [
    'apps.common',
    'apps.accounts',
    'apps.authentication',
    'apps.tenants',
    'apps.dashboard',
    'apps.compute',
    'apps.storage',
    'apps.networking',
    'apps.monitoring',
    'apps.quotas',
    'apps.billing',
    'apps.audit',
    'apps.admin_api',
    'apps.integrations.zadara',
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'
ASGI_APPLICATION = 'config.asgi.application'

# --------------------------------------------------------------------------- #
# Database — DATABASE_URL (Postgres in docker/prod), sqlite fallback for local
# --------------------------------------------------------------------------- #
DATABASES = {
    'default': env.db(
        'DATABASE_URL',
        default=f'sqlite:///{BASE_DIR / "db.sqlite3"}',
    )
}

# --------------------------------------------------------------------------- #
# Cache & Celery (Redis)
# --------------------------------------------------------------------------- #
REDIS_URL = env('REDIS_URL', default='redis://localhost:6379/0')

CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.redis.RedisCache',
        'LOCATION': REDIS_URL,
    }
    if env('USE_REDIS_CACHE', default=False)
    else {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}
}

CELERY_BROKER_URL = env('CELERY_BROKER_URL', default=REDIS_URL)
CELERY_RESULT_BACKEND = env('CELERY_RESULT_BACKEND', default=REDIS_URL)
CELERY_TASK_ALWAYS_EAGER = env('CELERY_TASK_ALWAYS_EAGER', default=False)

# Billing has no other source of history: the cloud meters nothing we can read,
# and a day the snapshotter misses can never be billed. Defaults to just after
# midnight UTC so a day is measured once, whole; the time is configurable
# because when it runs is an operational decision, not a code one.
BILLING_SNAPSHOT_HOUR = env.int('BILLING_SNAPSHOT_HOUR', default=0)
BILLING_SNAPSHOT_MINUTE = env.int('BILLING_SNAPSHOT_MINUTE', default=10)

CELERY_BEAT_SCHEDULE = {
    'capture-usage-snapshots': {
        'task': 'billing.capture_usage_snapshots',
        'schedule': crontab(hour=BILLING_SNAPSHOT_HOUR, minute=BILLING_SNAPSHOT_MINUTE),
    },
}

# --------------------------------------------------------------------------- #
# Auth / password validation
# --------------------------------------------------------------------------- #
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# --------------------------------------------------------------------------- #
# DRF + OpenAPI
# --------------------------------------------------------------------------- #
REST_FRAMEWORK = {
    'DEFAULT_RENDERER_CLASSES': ['apps.common.renderers.EnvelopeJSONRenderer'],
    'EXCEPTION_HANDLER': 'apps.common.exceptions.envelope_exception_handler',
    'DEFAULT_PAGINATION_CLASS': 'apps.common.pagination.EnvelopePagination',
    'PAGE_SIZE': 25,
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework.authentication.SessionAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
}

# Custom user model + Zadara-backed session auth.
AUTH_USER_MODEL = 'accounts.User'
AUTHENTICATION_BACKENDS = ['apps.authentication.backends.ZadaraSessionBackend']

SPECTACULAR_SETTINGS = {
    'TITLE': 'OpenCloud Console API',
    'DESCRIPTION': 'Application API over Zadara zCompute.',
    'VERSION': '1.0.0',
    'SERVE_INCLUDE_SCHEMA': False,
}

# --------------------------------------------------------------------------- #
# CORS / cookies (frontend on a separate origin)
# --------------------------------------------------------------------------- #
CORS_ALLOWED_ORIGINS = env('CORS_ALLOWED_ORIGINS')
CORS_ALLOW_CREDENTIALS = True

# The SPA reaches us through Next's /api/v1 proxy, so Django sees the browser's
# Origin (the frontend) next to its own host (the proxy target) — they never
# match and every unsafe request would fail the CSRF origin check without this.
CSRF_TRUSTED_ORIGINS = env('CSRF_TRUSTED_ORIGINS')
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_SAMESITE = 'Lax'

# --------------------------------------------------------------------------- #
# Zadara integration
# --------------------------------------------------------------------------- #
ZADARA_API_URL = env('ZADARA_API_URL', default='https://console.opencloud.uz')

# Where the password-reset e-mail should send people back to. Zadara substitutes
# the literal `$secret` in this template when it sends the mail.
FRONTEND_BASE_URL = env('FRONTEND_BASE_URL', default='http://localhost:3000')

# Billing / invoices. The seller is us, so it belongs in configuration rather
# than in a table someone could edit through the cabinet.
#
# VAT defaults to 0 — no tax unless someone asks for it. Charging tax nobody
# configured is worse than omitting it, and a rate of 0 drops the VAT rows from
# the document entirely rather than printing "НДС 0%". Uzbekistan's standard rate
# is 12% if it is ever needed; PRICES_INCLUDE_VAT then says whether the tariff
# already contains it (extracted) or not (added on top).
BILLING_VAT_RATE = env('BILLING_VAT_RATE', default='0')
BILLING_PRICES_INCLUDE_VAT = env.bool('BILLING_PRICES_INCLUDE_VAT', default=False)
BILLING_SELLER = {
    'name': env('BILLING_SELLER_NAME', default=''),
    'taxId': env('BILLING_SELLER_INN', default=''),
    'address': env('BILLING_SELLER_ADDRESS', default=''),
    'bank': env('BILLING_SELLER_BANK', default=''),
    'bankAccount': env('BILLING_SELLER_ACCOUNT', default=''),
    'bankCode': env('BILLING_SELLER_MFO', default=''),
    'director': env('BILLING_SELLER_DIRECTOR', default=''),
    'accountant': env('BILLING_SELLER_ACCOUNTANT', default=''),
    'phone': env('BILLING_SELLER_PHONE', default=''),
    'email': env('BILLING_SELLER_EMAIL', default=''),
}
PASSWORD_RESET_URL_TEMPLATE = env(
    'PASSWORD_RESET_URL_TEMPLATE',
    default=f'{FRONTEND_BASE_URL.rstrip("/")}/reset-password?secret=$secret',
)
ZADARA_HTTP_TIMEOUT = env.int('ZADARA_HTTP_TIMEOUT', default=15)
# Service (MSP read-only) account — loaded from secrets in real environments.
# Scoping to ZADARA_SERVICE_PROJECT yields the msp_admin (cluster-wide read) token.
ZADARA_SERVICE_ACCOUNT = env('ZADARA_SERVICE_ACCOUNT', default='')
ZADARA_SERVICE_USERNAME = env('ZADARA_SERVICE_USERNAME', default='')
ZADARA_SERVICE_PASSWORD = env('ZADARA_SERVICE_PASSWORD', default='')
ZADARA_SERVICE_PROJECT = env('ZADARA_SERVICE_PROJECT', default='')
# Fernet key used to encrypt cached Zadara tokens at rest (token vault).
TOKEN_VAULT_KEY = env('TOKEN_VAULT_KEY', default='')

# --------------------------------------------------------------------------- #
# I18N / static
# --------------------------------------------------------------------------- #
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# --------------------------------------------------------------------------- #
# Logging (mask sensitive data at the integration layer, not here)
# --------------------------------------------------------------------------- #
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {'simple': {'format': '[{levelname}] {name}: {message}', 'style': '{'}},
    'handlers': {'console': {'class': 'logging.StreamHandler', 'formatter': 'simple'}},
    'root': {'handlers': ['console'], 'level': env('DJANGO_LOG_LEVEL', default='INFO')},
}
