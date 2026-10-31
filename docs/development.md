# Development

How the repository is laid out, how to run and test it locally, and the conventions the code follows. To only try Kalkan, `make demo` is enough ([self-hosting.md](self-hosting.md)). The design is described in [architecture.md](architecture.md), and the reasons behind it in [decisions.md](decisions.md).

## Tools

| Tool | Version | Note |
| --- | --- | --- |
| Docker with the Compose plugin | Docker 27+, Compose v2.24+ | `docker compose up --wait` |
| make | 3.81+ | The version macOS ships is enough |
| uv | 0.12.24 | The version CI uses. It downloads Python itself |
| Python | 3.12.15 | `backend/.python-version`, the same patch release as the images |
| Node.js | 24 LTS (22 at least) | The panel (`web/`) and the edge Worker (`infra/cloudflare`) |

## First run

```sh
cp .env.example .env
make up                                   # build and start the local stack, wait until healthy
curl localhost:8000/healthz

cd backend && uv sync && cd ..            # dev and browser dependency groups
uv run --project backend pre-commit install
uv run --project backend playwright install chromium   # once, for the browser tests
make test
```

`make up` starts Postgres, Redis, fake-gcs (a Google Cloud Storage emulator), Mailpit, the fixture sites, `dev-proxy`, `api`, `worker-default`, `scheduler` and `ops`. Before the application starts, the one-off `migrate` service runs `alembic upgrade head`. On its first start, Postgres creates the roles `kalkan_owner`, `kalkan_api` and `kalkan_worker`, and the test database `kalkan_test` (`infra/compose/postgres/initdb/`, `backend/sql/bootstrap_roles.sql`). Every port binds to `127.0.0.1`. To move one, set `API_PORT`, `POSTGRES_PORT` and the like ([self-hosting.md](self-hosting.md#addresses-and-ports)).

Optional parts start separately:

| Target | Adds |
| --- | --- |
| `make up-browser` | `worker-browser` (Playwright's Chromium). Pages go through `dev-proxy`, which reaches only the fixture sites |
| `make up-ai` | `worker-ai`, making real Claude calls. Needs `ANTHROPIC_API_KEY` in `.env` and costs money |
| `make up-ct` | `certstream` and `ct-consumer`, connected to the real public CT logs (hundreds of certificates a second) |
| `make up-monitoring` | Prometheus and Alertmanager with the production alert rules; alarm e-mails go to Mailpit |

`make down` stops all of them and keeps the volumes. `make migrate` and `make seed` run on the host against the development database: `make seed` rebuilds the Mintera Bank demo and writes its logins to `.local/demo-credentials.json`. `make web-dev` serves the panel from source on http://localhost:3000 against `make up`'s API. If the API runs on another port, pass it on: `make web-dev API_ORIGIN=http://localhost:8010`.

## Repository layout

| Path | Contents |
| --- | --- |
| `backend/` | The Python package `kalkan` (`src/kalkan`), its tests, Alembic migrations (`alembic/versions`), `sql/bootstrap_roles.sql`, `openapi.json` |
| `web/` | The customer panel: Next.js, Tailwind, shadcn/ui, TanStack Query. Vitest unit tests next to the code, Playwright tests in `e2e/` |
| `fixtures/` | Fictional fixture sites (`sites/`, served by nginx), and recorded RDAP, CZDS, Web Risk, form and screenshot samples for tests |
| `eval/` | AI evaluation datasets and reports. Git-ignored: they contain screenshots of real sites |
| `deploy/selfhost/` | Docker Compose, Caddy, Postgres and Redis setup for one Linux server |
| `infra/compose/` | The local stack (`docker-compose.yml`, `dev-proxy/`, `postgres/`, `monitoring/`) and the reference VM's compose file and configuration (`vm/`: alert rules, Grafana dashboards, seccomp profile, scripts) |
| `infra/docker/` | `backend`, `browser` and `web` Dockerfiles, shared by every environment |
| `infra/terraform/`, `infra/cloudflare/`, `infra/vm/`, `infra/scripts/` | The optional Google Cloud reference deployment, its edge Worker, VM boot script and restore drill |
| `docs/` | These documents |

The backend package, by stage of the pipeline ([architecture.md §7](architecture.md)):

| Package | Responsibility |
| --- | --- |
| `discovery` | CT stream consumer, dnstwist permutations, CZDS zone files, the keyword matcher, the Public Suffix List copy |
| `enrichment` | DNS, RDAP, ASN, CDN detection, Web Risk, whitelist admission |
| `scanning` | HTTP pre-check, browser capture (Playwright), persisting evidence, proxy handling |
| `signals` | Visual, favicon, text and form similarity signals |
| `ai` | Claude client, prompts, cost tracking and budget, evaluation |
| `decision` | The decision rules and their application. The only place that decides about sending |
| `notices` | Channel planning, the sending gates, templates and legal texts, retractions |
| `followup` | Rechecks, takedown detection, escalation, inbound replies and webhooks |
| `reports` | Monthly reports: build, print hand-off, persist |
| `api`, `ops` | The customer API (FastAPI) and the internal ops panel (FastAPI, Jinja2, HTMX) |
| `workers`, `scheduler` | Dramatiq actors per queue, and the APScheduler process with the reconciler, outbox dispatcher and metrics exporter |
| `db`, `domain` | SQLAlchemy models, sessions, the transactional outbox, enums and the case state machine |
| `storage`, `email`, `net` | Object storage (GCS or filesystem), mail (SMTP or Postmark), the SSRF-safe HTTP client and the self-host egress proxy |
| `seed` | The Mintera Bank demo data and the local demo helpers |

`cli.py` is the operator CLI (`kalkan --help`), and `config.py` holds every setting with the services that need it (`kalkan check-config`).

## Make targets

`make help` lists them all.

| Target | What it does |
| --- | --- |
| `up`, `down`, `logs`, `ps`, `shell` | Local stack: start (and wait), stop every profile, follow logs, status, shell in `api` |
| `up-infra` | Only the services the tests use: Postgres, Redis, fake-gcs, Mailpit, fixture sites, dev-proxy |
| `up-browser`, `up-ai`, `up-ct`, `up-monitoring` | The optional profiles above |
| `demo`, `demo-browser`, `demo-info`, `demo-code`, `demo-down`, `demo-reset` | The one-command demo ([self-hosting.md](self-hosting.md#on-a-laptop)) |
| `test`, `test-unit`, `test-integration` | Backend tests: unit and integration, unit only, integration only (starts `up-infra`) |
| `e2e` | The pipeline end to end: fixture clone, Chromium, Mailpit, follow-up |
| `bench` | Matcher throughput (target: at least 2,000 domains a second) |
| `alert-test` | One alarm through Prometheus and Alertmanager to Mailpit |
| `load` | A simulated CT burst with fake network, browser and AI clients ([load-test.md](load-test.md)); `LOAD_ARGS="--minutes 5"` |
| `lint`, `fmt` | ruff, `ruff format --check` and mypy; formatting and safe ruff fixes |
| `migrate`, `seed` | Alembic upgrade on the development database; the demo data |
| `openapi`, `web-types` | Write `backend/openapi.json`; generate the panel's TypeScript types from it |
| `web-dev`, `web-lint`, `web-test`, `web-e2e` | The panel: dev server, eslint + prettier + tsc, Vitest, Playwright with axe |
| `edge-test` | The edge Worker's typecheck and tests |
| `infra-check` | Terraform fmt and validate, tflint, the VM and self-host compose files, the Caddyfile, Prometheus rules and their tests, the Alertmanager template, shellcheck. Everything runs in pinned Docker images and uses no cloud credentials |
| `eval` | AI evaluation on an exported dataset (`DATASET=`, `MODELS=`, `LIMIT=5`). Real API calls; costs money |

## Tests

| Layer | Where | Runs with | Count |
| --- | --- | --- | --- |
| Backend unit | `backend/tests/unit` | `make test-unit` | 1,029 |
| Backend integration | `backend/tests/integration` | `make test-integration` | 583 |
| Pipeline end to end | `backend/tests/e2e` | `make e2e` | 2 |
| Benchmark | `backend/tests/benchmark` | `make bench` | 1 |
| Alarm end to end | `backend/tests/alerting` | `make alert-test` | 1 |
| Panel unit (Vitest) | `web/src/**/*.test.ts` | `make web-test` | 38 in 12 files |
| Panel end to end (Playwright, axe) | `web/e2e` | `make web-e2e` | 24 in 9 files |
| Edge Worker (Vitest) | `infra/cloudflare/test` | `make edge-test` | 115 in 3 files |
| Alert rules (promtool) | `infra/compose/vm/prometheus/tests` | `make infra-check` | one case per rule |

The counts are from October 2026; `pytest --collect-only -q` prints the current backend total (1,616). The marker of a backend test comes from its directory (`tests/conftest.py`), and `--strict-markers`, `xfail_strict` and `filterwarnings = error` are on.

- **Unit tests** need no services and finish in seconds.
- **Integration tests** run against the compose services. They use their own database, `kalkan_test`, which is migrated from scratch once per session and emptied after each test, and Redis database 15, so they never touch the development data. If an old Postgres volume has no `kalkan_test`, recreate it: `make down && docker volume rm kalkan_postgres-data && make up`.
- **Browser tests** (`test_browser.py`, `test_capture_pipeline.py`, `test_report_print.py`) drive the host's Playwright Chromium, always with its sandbox, through `dev-proxy` at `127.0.0.1:18888`. When Chromium or dev-proxy is missing, they fail with a message that says how to fix it. Some kill and restart Chromium on purpose, so they take a minute or more.
- **No test leaves the machine.** HTTP is faked with `httpx2` mock transports and `responses`, AI tests replay recorded Claude responses (`tests/fixtures/ai`), and e-mail goes to Mailpit.
- **`make e2e`** injects the fixture clone as a candidate and follows it through the HTTP pre-check, a real Chromium capture through dev-proxy, the signals, a recorded AI triage, the decision and the approval, to notices in Mailpit. Then the site goes away and the clock moves on until the case is taken down. A second run checks that a dry-run customer only sends previews until dry-run is lifted.
- **`make web-e2e`** needs `make up` and re-seeds the demo. It builds and serves the panel, signs in as owner and viewer, checks every screen with axe, and writes screenshots and a sample monthly report (`report-sample.pdf`) to `web/test-results/`. Install Chromium for it once: `cd web && npx playwright install chromium`.
- **Notice template snapshots** (`tests/unit/test_notice_templates.py`) cover four languages. Rewrite them deliberately with `UPDATE_NOTICE_SNAPSHOTS=1`.

### Fixture sites and dev-proxy

`fixtures/sites/` holds fictional sites, served by an nginx container under `*.test` names: the official Mintera Bank site, a clone (password and card fields, a form posting to an external `/collect`), a lure page whose buttons lead to the clone and a third-party site, parked, empty, redirecting, mobile-only, bot-challenge, affiliate-review, redirect-loop and very heavy pages. [fixtures/sites/README.md](../fixtures/sites/README.md) lists each one with the pre-check result the tests expect.

- Inside compose the names resolve to `172.30.255.80`, which the workers may reach because `SSRF_ALLOW_CIDRS` admits that one address. From the host, use `127.0.0.1:18080` with a `Host:` header.
- `dev-proxy` (tinyproxy, `infra/compose/dev-proxy/`) stands in for the residential proxy. It forwards only `*.test` names and answers 403 for every other host: `curl -x http://127.0.0.1:18888 http://mintera-login.test/`. `worker-browser` sits on an internal network with only Redis and dev-proxy, so a request that skips the proxy has nowhere to go.
- After switching branches, git may replace the `fixtures/sites` directory and the container keeps serving the old one, so every site returns 404. Recreate it: `docker compose --env-file .env -f infra/compose/docker-compose.yml up -d --force-recreate fixture-sites dev-proxy`.

## API schema and panel types

The panel never hand-writes API types.

1. Change the API in `backend/src/kalkan/api`.
2. `make openapi` writes `backend/openapi.json`. Commit it.
3. `make web-types` generates `web/src/lib/api/schema.d.ts` with `openapi-typescript`. Commit it too.

CI regenerates both and fails if either differs from the committed file. The panel calls the API from the same origin: `next dev` and the production build rewrite `/api/*` to `API_ORIGIN`, so the session cookie works as in production.

## Continuous integration

`.github/workflows/ci.yml` runs on every pull request and on pushes to `main`:

| Job | Checks |
| --- | --- |
| `lint` | `make lint`, `openapi.json` up to date, the whitespace pre-commit hooks, gitleaks over the full history |
| `test-unit` | `make test-unit` |
| `benchmark` | `make bench` |
| `test-integration` | `make test-integration` with the compose services and Playwright's Chromium. On Ubuntu 24.04 the headless shell gets an AppArmor profile that allows user namespaces, so its sandbox stays on |
| `build` | The `backend`, `browser` and `web` images (not pushed) |
| `terraform` | `terraform fmt`, `validate` and tflint, with no backend and no credentials |
| `web` | API types up to date, eslint, prettier, tsc, Vitest, `next build` |
| `edge` | The edge Worker's typecheck and tests |
| `vm-files` | The reference VM's and the self-host compose files, the Caddyfile, Prometheus rules and their tests, the Alertmanager template, shellcheck |

`make e2e`, `make web-e2e` and `make alert-test` are not part of CI. Run them before changing the pipeline, the panel or the monitoring. Dependabot watches the uv, npm, Docker, Compose, Terraform and GitHub Actions dependencies. Actions are pinned by commit SHA and tools by exact version.

## Pre-commit

`uv run --project backend pre-commit install` installs the hooks (`.pre-commit-config.yaml`): end-of-file and trailing-whitespace fixers, gitleaks on the staged changes, and ruff, ruff format and mypy through uv, so the versions pinned in `backend/uv.lock` are used. The ruff and mypy hooks check the whole working tree, not only the staged files, so untracked files take part in the check.

## Conventions

- **Language.** Code, identifiers, comments, commit messages and UI text are in English. Commits follow Conventional Commits.
- **Style.** ruff (line length 100, a broad rule set that includes bandit's `S` rules) and mypy with every definition typed (`disallow_untyped_defs`, `warn_return_any` and friends; tests are exempt from the annotations). Comments explain why, and point to the design with `architecture.md §N`, `product.md §N` and decision ids like `K42`. Keep those references valid when you move text.
- **Decisions about sending live only in `backend/src/kalkan/decision/`.** AI output alone never triggers a notice. The rules are pure functions with a scenario table in `tests/unit/test_decision_rules.py`.
- **Untrusted input.** Scanned page content, inbound e-mail and RDAP data reach prompts only inside labeled blocks. Every HTTP request to a candidate site or a URL taken from one goes through `kalkan.net.safe_http`. The exceptions are listed in [architecture.md §10](architecture.md).
- **No evasion.** No code that solves CAPTCHAs, bypasses bot protection or uses stealth plugins. A challenge page sends the case to review.
- **Tenancy.** Every query that touches customer data is scoped by `customer_id`. The API runs as `kalkan_api` under row-level security. Never give it the worker or owner role to fix a problem.
- **Migrations.** Never edit an existing Alembic migration; add a new one. Write it so the previous release still runs on the new schema.
- **Settings.** A new environment variable goes into `.env.example`, `deploy/selfhost/.env.example` if the self-hosted stack needs it, and [architecture.md §26](architecture.md). Each container gets only the variables its process reads.
- **Decisions.** A design choice the documents do not cover gets a new `K` entry in [decisions.md](decisions.md).
- **Secrets.** Never commit secrets. Local demo and test credentials live in the git-ignored `.local/`. Locally `NOTICES_DRY_RUN=true`, and no test sends real mail.
