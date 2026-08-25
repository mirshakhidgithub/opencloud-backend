# Python matches the interpreter the test suite runs on, so production is not the
# first place a version difference shows up.
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# curl is here for the container healthcheck, not for the app.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Hash and compress the static files at build time so the manifest is already in
# the image. The values below exist only to satisfy the production settings'
# start-up checks while collectstatic runs — nothing here reaches a running
# container, which gets its real configuration from the environment.
RUN DJANGO_SETTINGS_MODULE=config.settings.prod \
    DJANGO_SECRET_KEY="build-time-only-$(python -c 'import secrets; print(secrets.token_urlsafe(48))')" \
    TOKEN_VAULT_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')" \
    USE_REDIS_CACHE=true \
    DATABASE_URL=postgres://build:build@build:5432/build \
    python manage.py collectstatic --noinput --clear

# Run as a non-root user: a container that only serves HTTP has no reason to be
# able to rewrite its own code.
RUN useradd --system --create-home --uid 10001 opencloud \
    && chown -R opencloud:opencloud /app \
    && mkdir -p /var/lib/celery \
    && chown opencloud:opencloud /var/lib/celery
USER opencloud

EXPOSE 8000

# Migrations run here rather than in a separate step so a deploy cannot start a
# process against a schema it does not match. Compose gives the worker and beat
# their own commands.
CMD ["sh", "-c", "python manage.py migrate --noinput && exec gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 3 --timeout 60 --access-logfile - --error-logfile -"]
