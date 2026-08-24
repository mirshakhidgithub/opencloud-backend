# OpenCloud Backend

Django + DRF application API over Zadara zCompute. PostgreSQL for app data,
Redis for cache + Celery broker, Celery for background sync. See
[`../backend-plan.md`](../backend-plan.md) for the full design.

Status: **M0 — infrastructure skeleton.** No business logic yet; `common`
provides the response envelope, error handler, RBAC base, health and OpenAPI.

## Contract (must match the frontend)
- Base path `/api/v1/*`
- Success: `{ "data": ..., "meta": ... }`  ·  Error: `{ "error": { "code", "message", "details" } }`
- Auth: httpOnly cookie; Zadara tokens live server-side only (never in the browser).

## Run with Docker (Postgres + Redis + Celery)
```bash
cp .env.example .env
docker compose up --build
# API:     http://localhost:8000/api/v1/health
# Swagger: http://localhost:8000/api/v1/docs
```

## Run locally (sqlite, no services)
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```

## Endpoints (M0)
- `GET /api/v1/health` — liveness + DB check
- `GET /api/v1/schema` — OpenAPI schema
- `GET /api/v1/docs` — Swagger UI

## Layout
```
config/            settings (base/dev/prod), urls, celery, wsgi/asgi
apps/common/       envelope renderer, exception handler, pagination, permissions, health
apps/*             empty apps (accounts, authentication, tenants, compute, …) filled per milestone
apps/integrations/zadara/   Zadara client (user + service modes) — added in M1/M2
```

## Milestones
M0 infra · **M1 auth+RBAC** · M2 compute+projects · M3 service mode+admin ·
M4 cache/sync · M5 storage/net/monitoring · M6 quotas/billing.

## Security notes
- Service (cloud-admin) Zadara account creds come from a secrets manager, not git.
- Cached tokens are encrypted at rest (Fernet, `TOKEN_VAULT_KEY`).
- Authorization is enforced per request by the user's role, regardless of the
  service token the backend may hold.

## Local cache (recommended)

The Zadara token vault and the short-lived MFA tickets live in Django's cache.
With the default `LocMemCache` they die with the process, so every `runserver`
autoreload forces a fresh sign-in. Start Redis and point the cache at it:

```bash
docker compose up -d redis     # published on 127.0.0.1:6379 for host runs
# .env
USE_REDIS_CACHE=true
REDIS_URL=redis://127.0.0.1:6379/0
```

## Usage snapshots (billing)

Billing has no other source of history: the cloud publishes no metering we can
read, so a day the snapshot does not run is a day that can never be billed.
This is the one scheduled job that has to keep running.

Run the worker and beat **on the host** for local work, so they share the dev
database and Redis with `runserver`:

```bash
docker compose up -d redis
celery -A config worker -l info --concurrency 2
celery -A config beat   -l info --schedule .run/celerybeat-schedule
```

The `worker`/`beat` services in `docker-compose.yml` are for real deploys: they
point at Postgres in the `db` container, so starting them alongside a sqlite
`runserver` would collect into a database the cabinet does not read.

Schedule is `BILLING_SNAPSHOT_HOUR`/`BILLING_SNAPSHOT_MINUTE` (UTC, default
00:10). To take one by hand — for a missed day, or to backfill the day it is
still possible to measure:

```bash
python manage.py capture_usage              # today
python manage.py capture_usage --date 2026-08-24
```

Prices are per unit per **month** and a period is charged in full; set them with
`manage.py set_tariff --price vcpu=46000 --price ssd_gb=1900 …` (omit
`--account` for the platform default, which the cabinet deliberately cannot
edit).
