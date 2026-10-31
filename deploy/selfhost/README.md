# Self-hosting Kalkan

Run Kalkan on one Linux server with Docker Compose. You don't need GCP, Cloudflare, Postmark or any other paid account. Everything is built from this repository and reached through Caddy, which gets the HTTPS certificates itself.

If you only want to try Kalkan, use `make demo` from the repository root instead: it runs on a laptop with no domain and no SMTP server.

## What runs

| Service | Role | Reachable from |
| --- | --- | --- |
| `caddy` | HTTPS for `APP_DOMAIN` and `EVIDENCE_DOMAIN`. It sends `/api/*` and `/healthz` to `api`, the rest of the app domain to `web`, and the whole evidence domain to `api`. It adds `X-Edge-Auth` to every request. | The internet (80, 443) |
| `api` | Customer API and evidence pages (FastAPI) | Caddy |
| `web` | Customer panel (Next.js) | Caddy |
| `worker-default` | Discovery, enrichment, decisions, notices, follow-up (Dramatiq) | Nothing (no ports) |
| `scheduler` | Periodic jobs and the metrics exporter | Nothing |
| `ops` | Operations panel for operators | `127.0.0.1:8081` on the server only |
| `postgres`, `redis` | State and job queues. Redis requires a password; worker-browser has its own Redis user, limited to its queues. | The compose network |
| `migrate` | One-off: `alembic upgrade head` before the application starts | — |

Optional parts are selected with `COMPOSE_PROFILES` in `.env`:

| Profile | Adds | Needs |
| --- | --- | --- |
| `browser` | `worker-browser` (Chromium screenshots, the monthly report PDFs) and `egress-proxy` | About 4 GB more RAM (8 GB total) |
| `ai` | `worker-ai` (Claude triage, confirmation, notice text, reply classification) | `ANTHROPIC_API_KEY`; it costs money (`AI_MONTHLY_BUDGET_USD` caps it) |
| `ct` | `certstream` and `ct-consumer`: new certificates from the public Certificate Transparency logs | Bandwidth and about 1 CPU |
| `monitoring` | Prometheus, Alertmanager (alarms by SMTP), Grafana and node-exporter, with the reference deployment's alert rules and dashboards | — |

Without `ai`, cases that would go to the AI go straight to the review queue (reason `ai_disabled`). Notices then use a fixed explanation paragraph, and replies wait for an operator. Without `browser`, capture jobs wait; nothing is lost.

## Requirements

- A Linux server with Docker Engine 28+ and Docker Compose v2.33+. The `monitoring` profile needs these versions; the rest runs on Docker 27 and Compose 2.24.
- At least 2 vCPU, 4 GB RAM and 20 GB of disk; with `browser`, about 4 GB more (8 GB total).
- Two DNS names pointing at the server, for example `app.example.com` and `evidence.example.com`.
- Ports 80 and 443 open to the internet. Caddy needs them to obtain the certificates.
- An SMTP server that accepts mail from your sender address. Any provider works: your own, a transactional mail service, or a mailbox with SMTP access.
- Docker set up so that Caddy sees each visitor's real address. Published ports of an IPv6 client (and some hairpin setups) otherwise go through Docker's userland proxy, and every such visitor arrives with the Docker gateway's address. The evidence pages' rate limit then counts them all as one client, so one abuser can lock out every abuse desk that uses IPv6. Set `"userland-proxy": false` and `"ip6tables": true` (with `"ipv6": true` where you serve IPv6) in `/etc/docker/daemon.json` and restart Docker, or run Caddy with `network_mode: host`.

## Install

```sh
git clone <this repository> kalkan && cd kalkan/deploy/selfhost
./init-env.sh            # .env with random passwords and keys (chmod 600); never overwrites
$EDITOR .env             # APP_DOMAIN, EVIDENCE_DOMAIN, ACME_EMAIL, SMTP_URL, MAIL_FROM, ...
docker compose up -d --build
docker compose ps        # every service healthy, migrate exited 0
```

The first build takes several minutes; the browser image alone is about 6 GB. Caddy requests both certificates when it starts, and `https://APP_DOMAIN/login` should answer within a minute.

Create the first operator. The password and the TOTP secret are shown once; scan the `otpauth://` link into an authenticator app.

```sh
docker compose exec ops kalkan staff create you@example.com --generate-password
```

To choose the password yourself without typing it on the command line, use `--password-stdin` (`printf '%s\n' "$PASSWORD" | docker compose exec -T ops kalkan staff create ...`).

Open the ops panel through an SSH tunnel: run `ssh -L 8081:127.0.0.1:8081 you@server`, then visit `http://localhost:8081`. From there you create customers, brands, official domains and whitelists, and invite the customer's users. The invitation e-mail links to the panel at `APP_DOMAIN`.

The same steps are available as CLI commands: `docker compose exec ops kalkan --help`.

## Settings

Everything is in `.env`; `.env.example` explains each setting. The most important ones:

- **Mail.** `EMAIL_PROVIDER=smtp` with `SMTP_URL`:
  - `smtp+starttls://user:password@host:587`
  - `smtps://user:password@host:465`

  The user and password must be percent-encoded. A password over plain `smtp://` is refused, and plain `smtp://` itself is accepted only for a relay on this server or its private network (loopback, a private address or a compose service name) unless `SMTP_ALLOW_PLAINTEXT=true`. `EMAIL_PROVIDER=postmark` with `POSTMARK_SERVER_TOKEN` uses Postmark's API instead. Replies and bounces are tracked only through Postmark's inbound and bounce webhooks (`/api/webhooks/postmark/*`, protected by `POSTMARK_WEBHOOK_BASIC_AUTH`). With plain SMTP the notices go out but replies are not read in. Each notice's `Reply-To` is `case+<number>@REPLY_DOMAIN`: receive mail for that domain, or leave `REPLY_DOMAIN` empty so replies reach the `MAIL_FROM` mailbox.
- **Real notices.** `NOTICES_DRY_RUN=true` (the default) sends every abuse notice to `DRY_RUN_REDIRECT_EMAIL` instead of the hoster or registrar. Set it to `false` only when you are ready to send real notices.
- **Storage.** Evidence (screenshots, HTML, RDAP and DNS answers) and reports are files in the `objects` volume (`STORAGE_BACKEND=filesystem`). They are written once and never changed. Object keys keep the `gs://<bucket>/<path>` form as logical names, so a later move to a bucket keeps the database valid.
- **Secrets.** `init-env.sh` generates them. A `change-me` placeholder left in `.env` stops Postgres's first start and the api (which also wants an `EDGE_SHARED_SECRET` of at least 32 characters), and Compose refuses to start Grafana without `GRAFANA_ADMIN_PASSWORD`. `TOTP_ENCRYPTION_KEY` encrypts every TOTP secret. Keep a copy: without it every user and operator has to set up TOTP again. Never change the database passwords in `.env` after the first start; the roles were created with them.

After changing `.env`, run `docker compose up -d` again. Only the containers whose settings changed are recreated.

## The browser and its egress proxy

Chromium opens untrusted sites, so it gets no route to the internet. `worker-browser` sits on an internal Docker network with Redis and `egress-proxy`, and every page goes through the proxy (`python -m kalkan.net.egress_proxy`). The proxy:

- resolves each host once;
- refuses private, loopback, link-local, CGNAT and metadata addresses with the same rules as the backend's SSRF-safe HTTP client;
- connects to the address it checked, so DNS rebinding cannot redirect it;
- allows only ports 80 and 443.

The proxy has no authentication and must stay on the internal network. Pages leave from your server's own IP address. The cloud reference uses residential proxies per country instead, which this setup has no equivalent for.

The monthly report is printed from `http://web:3000` over a second internal network that only `worker-browser` and `web` join.

On **Ubuntu 23.10 and later**, AppArmor restricts unprivileged user namespaces, which Chromium's sandbox needs. The sandbox is never turned off, so captures then fail with a sandbox error. Allow user namespaces with one of these:

- `sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0`. To keep it after a reboot, also add the line to `/etc/sysctl.d/60-kalkan.conf`.
- A dedicated AppArmor profile for the container runtime.

Optional defense in depth: firewall rules (iptables `DOCKER-USER`) that drop traffic from the browser network to private ranges. `infra/compose/vm/bin/firewall.sh` shows how the reference VM does it.

## Monitoring

With the `monitoring` profile, the UIs listen on `127.0.0.1` (use an SSH tunnel):

- Prometheus: `:9090`
- Alertmanager: `:9093`
- Grafana: `:3001`, user `admin`, password `GRAFANA_ADMIN_PASSWORD`

To get alarms by e-mail, set these in `.env`:

- `ALERT_SMTP_SMARTHOST` (`host:port`)
- `ALERT_SMTP_USERNAME` and `ALERT_SMTP_PASSWORD`
- `ALERT_MAIL_FROM`
- `OPS_ALERT_EMAIL`

Without them, alarms are dropped and the Alertmanager log says so.

`NoCtEvents` fires while the `ct` profile is off. Either turn on `ct` or silence that alarm in Alertmanager.

## Backups and upgrades

Back up the database and the `objects` volume together:

```sh
umask 077                                   # the backups hold every customer's data
BACKUPS=/var/backups/kalkan                 # outside the repository
sudo install -d -m 700 -o "$USER" "$BACKUPS"
docker compose exec -T postgres pg_dump -U postgres -Fc kalkan > "$BACKUPS/kalkan-$(date +%F).dump"
docker run --rm -v kalkan-selfhost_objects:/data:ro -v "$BACKUPS":/backup alpine \
  sh -c "umask 077 && tar czf /backup/objects-$(date +%F).tar.gz -C /data ."
```

The files go to a directory outside the git checkout and are readable only by you (the objects archive by root). Also keep `.env` somewhere safe, for example in a password manager. It holds the only copy of `TOTP_ENCRYPTION_KEY`.

Restoring these backups, and a drill that checks them without touching the running stack, are in [docs/operations.md](../../docs/operations.md#restore-drill).

To upgrade, run `git pull && docker compose up -d --build`. `migrate` applies new database migrations before the application starts again.

`docker compose down` stops everything and keeps the data. `docker compose down -v` deletes the database and every stored object.

## Troubleshooting

- `docker compose logs -f api worker-default`: the logs are JSON lines. The `local` logging driver rotates them (5 × 10 MB per container).
- `docker compose exec worker-default kalkan check-config --service worker-default` lists missing settings without printing their values. Run it in the service you check: each container gets only its own settings.
- **Certificates.** If Caddy cannot get a certificate, check that both DNS names resolve to this server, that ports 80 and 443 are reachable, and look at `docker compose logs caddy`.
- **403 "Requests must come through the edge".** The request reached the api without passing Caddy. Caddy adds `X-Edge-Auth`.
