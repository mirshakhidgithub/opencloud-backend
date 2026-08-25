# Deploying Open Cloud (cabinet.opencloud.uz)

The server already runs **PostgreSQL** and **nginx**, so neither is containerised
here. What ships in Docker is the software we write plus Redis:

```
                 internet
                    │  443/tcp
              ┌─────▼──────┐
              │ nginx (host)│  TLS, one origin: cabinet.opencloud.uz
              └──┬───────┬──┘
     /api/v1/*   │       │   everything else
     /static/*   │       │
        ┌────────▼──┐  ┌─▼─────────────┐
        │ web :8000 │  │ frontend :3000│      both published on 127.0.0.1 only
        │ (gunicorn)│  │ (next start)  │
        └──┬─────┬──┘  └───────┬───────┘
           │     │             │ server-side calls → http://web:8000
     ┌─────▼─┐ ┌─▼──────┐      │
     │ redis │ │ worker │      └─ docker network `opencloud` (shared)
     │       │ │ beat   │
     └───────┘ └────┬───┘
                    │ 5432 over the docker gateway
            ┌───────▼────────┐
            │ PostgreSQL (host)│
            └─────────────────┘
```

Two compose stacks, one per repository, joined by an external network. They are
separate because the repositories are, and because the console can be rebuilt
without restarting anything that holds state.

---

## 1. Server prerequisites

```bash
docker --version && docker compose version   # 20.10+ for host-gateway
psql --version
nginx -v
```

Create the shared network once. The subnet is pinned so the database can be
opened to exactly this network and nothing wider:

```bash
docker network create --subnet 172.28.0.0/16 opencloud
```

## 2. PostgreSQL on the host

```bash
sudo -u postgres createuser opencloud --pwprompt
sudo -u postgres createdb opencloud --owner=opencloud
```

The containers reach the database through the docker gateway, so Postgres has to
listen on more than loopback and accept that network. Check the first before
changing it — a host already serving other applications usually listens widely
already, and `listen_addresses` is the one setting here that needs a **restart**,
which interrupts every other database on the box:

```bash
sudo -u postgres psql -tAc 'show listen_addresses'    # '*' → nothing to change
```

The access rule is narrow on purpose — this role, this database, this docker
network — and `pg_hba.conf` only needs a **reload**, so nobody else's connections
are dropped:

```bash
# /etc/postgresql/<version>/main/pg_hba.conf
host    opencloud    opencloud    172.28.0.0/16    scram-sha-256

sudo -u postgres psql -c 'select pg_reload_conf()'
```

Make sure the host firewall does not expose 5432 to the world; only the docker
bridge needs it.

## 3. Backend

```bash
git clone <backend repo> /srv/opencloud/backend
cd /srv/opencloud/backend
cp .env.example .env
```

Fill `.env`. The values that must not be left at a default — production settings
refuse to start otherwise, which is the point:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(64))"                       # DJANGO_SECRET_KEY
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # TOKEN_VAULT_KEY
```

```ini
DJANGO_DEBUG=false
DJANGO_ALLOWED_HOSTS=cabinet.opencloud.uz,127.0.0.1
CORS_ALLOWED_ORIGINS=https://cabinet.opencloud.uz
CSRF_TRUSTED_ORIGINS=https://cabinet.opencloud.uz
FRONTEND_BASE_URL=https://cabinet.opencloud.uz
DATABASE_URL=postgres://opencloud:PASSWORD@host.docker.internal:5432/opencloud
ZADARA_SERVICE_ACCOUNT=...        # service (cloud-admin) credentials
ZADARA_SERVICE_USERNAME=...
ZADARA_SERVICE_PASSWORD=...
ZADARA_SERVICE_PROJECT=...
BILLING_SELLER_NAME=...           # and INN / ADDRESS / ACCOUNT — see §7
```

`FRONTEND_BASE_URL` is the one that fails quietly: Zadara puts it in the
password-reset e-mail, so a leftover `localhost:3000` reaches real inboxes.

```bash
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml ps          # web must become healthy
curl -fsS http://127.0.0.1:8010/api/v1/health
```

The stacks publish on **8010** and **3010**, not 8000 and 3000, because this host
already runs other applications on those. `WEB_PORT` / `FRONTEND_PORT` move them,
and the nginx upstreams must say the same.

Migrations run when `web` starts, so a deploy can never serve against a schema it
does not match.

## 4. Frontend

`next build` wants a couple of gigabytes of RAM. On a host shared with other
people's processes, build the image somewhere else and load it instead of letting
the OOM killer choose a victim:

```bash
# elsewhere — --platform matters: an arm64 image from an Apple Silicon machine
# loads on the server without complaint and then restart-loops with exit 255
docker build --platform linux/amd64 -t opencloud-frontend:prod .
docker save opencloud-frontend:prod | gzip -1 | ssh HOST 'gunzip | sudo docker load'

# on the host — no --build, so it uses the image that was just loaded
git clone <frontend repo> /srv/opencloud/cabinet-tz
cd /srv/opencloud/cabinet-tz
docker compose -f docker-compose.prod.yml up -d --build   # no .env needed here:
                                                         # the compose sets both
                                                         # values the image wants
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:3010/login   # 200
```

`NEXT_PUBLIC_*` is compiled into the browser bundle, so changing either of those
two needs `up -d --build`, not a restart.

## 5. nginx and TLS

TLS is the existing `*.opencloud.uz` wildcard — nothing to issue, nothing to
renew, and the site file already points at where this host keeps it
(`/etc/ssl/opencloud.crt` + `.key`), so it is copied as-is.

```bash
sudo cp deploy/nginx/cabinet.opencloud.uz.conf /etc/nginx/sites-available/
sudo ln -s /etc/nginx/sites-available/cabinet.opencloud.uz.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

# The chain, not just the leaf: nginx sends only what this file contains.
grep -c 'BEGIN CERTIFICATE' /etc/ssl/opencloud.crt        # more than 1
openssl s_client -connect cabinet.opencloud.uz:443 -servername cabinet.opencloud.uz \
  </dev/null 2>/dev/null | grep -E 'Verify return code'   # 0 (ok)
```

`X-Forwarded-Proto` is not optional. nginx owns the http→https redirect (the
compose sets `SECURE_SSL_REDIRECT=false`, because a redirect inside the stack
would break the console's own server-side calls), but Django still needs the
header to know a request arrived over TLS — it marks cookies and builds absolute
URLs from it.

## 6. Check it is actually working

```bash
curl -sI https://cabinet.opencloud.uz/login | head -1                  # 200
curl -s  https://cabinet.opencloud.uz/api/v1/health                    # {"data": ...}
```

Then in a browser: sign in with a real Zadara account, switch project, open
Compute — the VM list is the first screen that proves the service token, the
vault and Redis are all working together.

## 7. Before anyone is billed

- **Seller requisites.** `BILLING_SELLER_{NAME,INN,ADDRESS,ACCOUNT}` must be
  filled or the API refuses to issue an invoice (`requisites_incomplete`). VAT is
  off (`BILLING_VAT_RATE=0`) unless the invoice has to carry tax.
- **Tariffs.** An empty price list produces documents with zeroes:
  `docker compose -f docker-compose.prod.yml exec web python manage.py set_tariff --help`
- **The daily snapshot.** It is the only source of billing history and a missed
  day can never be recovered. Take one immediately so today is measured rather
  than waiting for beat:

  ```bash
  docker compose -f docker-compose.prod.yml exec web python manage.py capture_usage
  ```

  `BILLING_SNAPSHOT_HOUR/MINUTE` are UTC; Tashkent is UTC+5. Check the morning
  after that yesterday is there — beat running is not proof that it ran.

## 8. Backups

Invoices are the only data in the system that cannot be rebuilt from the cloud:
everything else is a cache of what Zadara already knows, but an issued document
carries a number, frozen prices and both parties' requisites. Back the database
up on the host, where Postgres lives:

```bash
# /etc/cron.d/opencloud-backup
30 1 * * * postgres pg_dump -Fc opencloud > /var/backups/opencloud/$(date +\%F).dump
```

Keep at least a month, and restore one into a scratch database once — a backup
nobody has restored is a hypothesis.

## 9. Updating

```bash
cd /srv/opencloud/backend   && git pull && docker compose -f docker-compose.prod.yml up -d --build
cd /srv/opencloud/cabinet-tz && git pull && docker compose -f docker-compose.prod.yml up -d --build
```

Rolling back is the same command on an earlier commit. A migration that has
already applied does not roll back with it — check `git log` for new migrations
before reverting past one.

## 10. Logs

```bash
docker compose -f docker-compose.prod.yml logs -f web worker beat
sudo tail -f /var/log/nginx/cabinet.opencloud.uz.error.log
```
