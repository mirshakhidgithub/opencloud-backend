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

## Tests

```bash
pytest
```

Hermetic: an in-memory database and cache, and nothing reaches the cloud —
anything that would is patched at the `apps.integrations.zadara` boundary, not
at the `requests` level, so a change in our own client cannot slip through a
green run.

What they cover, and why those things:

- **The account boundary.** It has broken once — `resolve_app_role` matched admin
  hints as substrings, so an ordinary `tenant_admin` became ADMIN and
  `/admin/resources` served 155 machines across 21 unrelated companies. Tested
  from the outside, through HTTP, because the boundary is a property of the
  response.
- **Cache tenant isolation.** The response cache is keyed on the token, not the
  path; a path-only key would hand one customer another's machine list.
- **Billing arithmetic.** A full month costs exactly the quoted price in a 28-,
  29-, 30- or 31-day month; every row multiplies out; VAT is added or extracted
  according to configuration, never guessed.
- **Invoice freezing.** An issued document must not move when a price is
  corrected, and re-issuing must return the original rather than rebuild it.
- **The concurrent fetcher.** One refusal must not take the other sources with
  it, and the sources must genuinely run at the same time.

The suite was checked by mutation: the substring-role bug, a path-only cache key,
a missing account filter and a flat ÷30 in the billing maths were each
reintroduced and each turned the suite red. One mutation — recreating an issued
invoice instead of returning it — passed at first, which is why
`test_reissuing_after_a_price_change_returns_the_original_figures` exists.

## Postgres

Local development runs on sqlite and never sets `DATABASE_URL`. That is fine for
one developer and wrong for a deployment: web, the Celery worker and beat are
concurrent writers, and sqlite serialises them until one gets a lock error.

Postgres is **not** a performance question here — measured, the database never
appears in the hot path (see the latency notes in the commit history: the only
endpoint reading our own tables answered in ~200 ms while the cloud-backed ones
took seconds).

```bash
docker compose up -d db          # published on 127.0.0.1:5432 for host runs
export DATABASE_URL=postgres://opencloud:opencloud@127.0.0.1:5432/opencloud
python manage.py migrate
```

Verified: all migrations apply cleanly, the whole suite passes against it
(`DJANGO_SETTINGS_MODULE=config.settings.test_pg pytest`), and the app serves
reads and writes from it end to end.

**Moving existing local data across.** The usage snapshots are the one thing that
cannot be recreated — the cloud keeps no inventory history — so they have to come
with you:

```bash
python manage.py dumpdata accounts billing audit tenants \
  --natural-foreign --natural-primary --indent 2 -o local-data.json
DATABASE_URL=postgres://... python manage.py loaddata local-data.json
```

**Switch web, worker and beat together.** They are all writers; leaving one on
sqlite while the others move would split writes across two databases and the
split would not announce itself.

`psycopg[binary]` is pinned at 3.2.13 rather than 3.2.9: 3.2.9 publishes no
wheel for Python 3.14, which is what the local venv runs, so the older pin could
not be installed outside the 3.12 container image.
