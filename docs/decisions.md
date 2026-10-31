# Decision log

This log records the design decisions behind Kalkan, one entry per decision, in the order they were made. Each entry says what was decided, why, and which alternatives were considered and rejected.

- **Ids are stable.** Code comments, configuration and tests cite decisions as `K<number>` (for example `# K82`). An id is never reused or renumbered.
- **Entries are not deleted.** When a later decision replaces an earlier one, the earlier entry keeps its text and gets a **Status** line pointing to its replacement.
- **References.** `§N` refers to a section of [architecture.md](architecture.md), whose numbered sections (1 to 26) code comments cite as `architecture.md §N` or `§N`. `product.md §N` refers to the product rules in [product.md](product.md).
- **Gaps.** The numbering has gaps; retired ids are not reused.
- **Portfolio-era decisions.** Entries from [K177](#k177--one-squashed-initial-migration) onward were made when Kalkan became a self-hostable portfolio project.

## Index by topic

| Topic | Decisions |
| --- | --- |
| Foundations and repository | K1, K2, K3, K5, K10, K15, K16, K29, K30, K31, K32, K33, K35, K36, K51, K52, K75, K177 |
| Tenancy, roles and security | K6, K11, K23, K26, K47, K69, K73, K82, K102, K104, K107, K108, K117, K118, K149, K154 |
| Data model and case lifecycle | K17, K24, K27, K37, K39, K40, K41, K42, K44, K45, K48, K131, K143 |
| Discovery and admission | K13, K54, K58, K59, K60, K61, K62, K66, K106 |
| Enrichment | K22, K57, K63, K64, K65, K67, K68 |
| HTTP pre-check | K71, K72, K74, K109 |
| Browser capture | K70, K90, K91, K92, K93, K94, K95, K96, K97, K98, K99, K100, K101, K103, K105 |
| Similarity signals | K110, K111, K112, K113, K115, K116 |
| AI layer | K7, K114, K123, K124, K125, K126, K127, K128, K129, K130, K184 |
| Decision engine and review | K119, K120, K121, K122, K132, K133, K134, K135 |
| Notices | K9, K12, K21, K28, K43, K56, K136, K137, K138, K139, K140, K141, K181, K182 |
| Follow-up and replies | K19, K142, K144, K145, K146, K147, K148 |
| Customer API and web panel | K18, K150, K151, K152, K153, K155, K156, K157, K158, K159, K160, K161 |
| Reports | K167, K168, K169, K170, K171 |
| Demo data | K46, K49, K50, K53, K55, K175, K179, K185 |
| Monitoring and load | K34, K83, K103, K172, K173, K174, K176 |
| Self-hosting | K180, K181, K183, K185, K186 |
| Reference cloud deployment | K4, K14, K76, K77, K78, K79, K80, K81, K82, K84, K85, K86, K87, K88, K89, K187 |
| Portfolio version | K177 to K187 |

---

### K1 · Monorepo

- **Decision:** Backend, web panel, infrastructure and docs live in one repository.
- **Why:** One developer, shared docs, one CI pipeline.
- **Alternatives:** Separate repositories per component.

### K2 · Synchronous SQLAlchemy and psycopg everywhere

- **Decision:** SQLAlchemy 2 and psycopg 3 are used synchronously in every process, the API included.
- **Why:** Dramatiq is synchronous. One session model everywhere keeps the code simple, and the traffic is small.
- **Alternatives:** Async SQLAlchemy in the API.

### K3 · API to worker hand-off through a database outbox

- **Decision:** The API never talks to Redis. Work for the workers is written to `jobs_outbox` in the same transaction as the change that causes it, and the scheduler's dispatcher sends it to Dramatiq.
- **Why:** In the cloud reference the API runs on Cloud Run and Redis on a VM, and reaching it would need VPC egress. The state is in the database anyway, so a job is never lost between commit and queue.
- **Alternatives:** Direct VPC egress from Cloud Run to Redis.

### K4 · Cloudflare Worker in front of Cloud Run (reference deployment)

- **Decision:** In the cloud reference, a Cloudflare Worker serves the public hosts and forwards to Cloud Run.
- **Why:** Cloud Run domain mapping is not available in every region (and is in preview where it is), and a Global Application Load Balancer adds a fixed monthly cost.
- **Alternatives:** Global Application Load Balancer.
- **Status:** Details in K84. Self-hosted servers use Caddy instead (K186).

### K5 · Ops panel with FastAPI, Jinja2 and HTMX, local only

- **Decision:** The internal operations panel is server-rendered (FastAPI, Jinja2, HTMX) and listens on localhost only.
- **Why:** It stays in Python, needs no build step, and is never exposed to the internet.
- **Alternatives:** An `/ops` section inside the Next.js panel.

### K6 · The browser worker has no cloud credentials and no database

- **Decision:** `worker-browser` gets no database URL and no cloud identity. It writes captures to a hand-off folder, and a separate job in `worker-default` stores them.
- **Why:** Chromium opens hostile pages. Without credentials, a compromised browser cannot reach the metadata server's tokens or the database.
- **Alternatives:** One worker with credentials that captures and stores.

### K7 · Haiku 5.5 for the cheap AI tasks

- **Decision:** Triage and reply classification use Claude Haiku 5.5 (`AI_TRIAGE_MODEL`, `AI_REPLY_MODEL`). Confirmation and notice text use Sonnet 5.5.
- **Why:** At the October 2026 prices it is about ten times cheaper than the alternative. The evaluation harness (K129) checks the quality.
- **Alternatives:** Haiku 4.5.

### K9 · Postmark for e-mail

- **Decision:** Postmark handles outgoing mail, inbound replies and bounces.
- **Why:** One provider and simple webhooks.
- **Alternatives:** Amazon SES.
- **Status:** Refined by K139. In the portfolio version SMTP is the default and Postmark is optional (K181).

### K10 · Infrastructure as Terraform

- **Decision:** The cloud reference is described in Terraform.
- **Why:** A readable plan before anything changes, and repeatable setups.
- **Alternatives:** gcloud scripts.

### K11 · TOTP for every user

- **Decision:** Every customer user and every operator must use TOTP.
- **Why:** It is a security product, and the number of users is small.
- **Alternatives:** TOTP only for owners.

### K12 · Registrar after 48 hours

- **Decision:** For phishing, the registrar was to be contacted 48 hours after the first notice.
- **Why:** This protected the registrar channel's reputation.
- **Alternatives:** Notify every channel in parallel at once.
- **Status:** Replaced by K56. The registrar is now part of the first wave for phishing.

### K13 · CZDS zones streamed, no full diff

- **Decision:** CZDS zone files are streamed through the matcher. No full day-to-day diff is kept.
- **Why:** A full diff costs disk space and time, and the CT logs already catch most new names.
- **Alternatives:** A daily full diff of every zone.
- **Status:** Which zones are read is set by K59 and K106.

### K14 · Evidence retention

- **Decision:** In the cloud reference, case evidence and stored messages are deleted on day 365 by a bucket lifecycle rule. There is no retention lock.
- **Why:** This follows the product's retention rule and keeps storage costs bounded.
- **Alternatives:** Keep everything forever.
- **Status:** Only `cases/` and `messages/` expire (K44). The filesystem backend (K180) never deletes evidence.

### K15 · No staging environment

- **Decision:** There is no separate staging environment.
- **Why:** Cost. A dry-run test customer in production covers the end-to-end checks (K24).
- **Alternatives:** A separate project.

### K16 · Row level security on tenant tables

- **Decision:** Every tenant table has a Postgres RLS policy in addition to the application's `customer_id` filters.
- **Why:** A second layer against application bugs.
- **Alternatives:** Application filters only.

### K17 · `unrelated` dismisses the case

- **Decision:** An `unrelated` result dismisses the case instead of watching it.
- **Why:** The panel's design shows such cases as "Dismissed". The monthly light check of dismissed cases (K143) is enough.
- **Alternatives:** Watch every seven days.

### K18 · Evidence files are streamed through the API

- **Decision:** Stored files reach the panel through API endpoints. There are no signed URLs.
- **Why:** The same path works locally and in the cloud, no URL can leak, and no extra IAM role is needed.
- **Alternatives:** V4 signed URLs.

### K19 · Warning pages are not takedowns

- **Decision:** A Cloudflare or browser warning page in front of a site that is still up does not count as a takedown. Follow-up records it as `warning`.
- **Why:** Counting warnings would inflate "Taken down" and the takedown rate. The warning is shown separately.
- **Alternatives:** Treat a warning as a takedown.

### K21 · Customer alerts by e-mail only

- **Decision:** Customer alerts go by e-mail only.
- **Why:** Scope.
- **Alternatives:** A Telegram bot.

### K22 · RDAP with whoisit for domains, IPs and ASNs

- **Decision:** `whoisit` handles every RDAP lookup: domains, IP addresses and autonomous systems.
- **Why:** One client, testable at the HTTP layer.
- **Alternatives:** `ipwhois` for IPs.

### K23 · No BYPASSRLS; a permissive worker policy

- **Decision:** No role has `BYPASSRLS`. `kalkan_worker` gets a permissive `worker_all` policy on each tenant table, and the application roles are ordinary roles created by SQL (`backend/sql/bootstrap_roles.sql`).
- **Why:** Managed Postgres (Cloud SQL) does not grant `BYPASSRLS`, and least privilege is better anyway.
- **Alternatives:** A role with `BYPASSRLS`.

### K24 · Dry-run per customer

- **Decision:** Besides the global `NOTICES_DRY_RUN`, every customer has a `dry_run` flag (default true). In dry-run no external send API is called. Previews go to `DRY_RUN_REDIRECT_EMAIL`.
- **Why:** Customers can be switched to real sending one at a time. A test customer stays in dry-run permanently, and a database constraint enforces it.
- **Alternatives:** Only the global switch.

### K26 · Pre-tenant resolution through SECURITY DEFINER functions

- **Decision:** Sign-in, session tokens, invitations, password resets, evidence links and report tokens are resolved by `SECURITY DEFINER` functions that return only ids.
- **Why:** These lookups happen before the tenant is known. The functions make that possible without opening RLS.
- **Alternatives:** Keep the identity tables outside RLS.

### K27 · A dry-run notice is a preview

- **Decision:** A dry-run notice is a preview. The case stays `approved` with `blocked_reason=dry_run`. When dry-run is switched off, the real first wave is planned.
- **Why:** The panel, the metrics and the follow-up then reflect only real notices.
- **Alternatives:** Move the case to `notified` on a preview.

### K28 · The global send switch starts off

- **Decision:** The global send switch (`settings.sending_enabled`) is created as false, so real sending is stopped until an operator turns it on. Previews are not affected.
- **Why:** One more layer against accidental real notices.
- **Alternatives:** Default true.

### K29 · Documentation layout

- **Decision:** Code lives at the repository root, and the documentation in `docs/`.
- **Why:** One place for the docs, separate from the code they describe.
- **Alternatives:** Docs spread next to the code.

### K30 · Each queue belongs to exactly one worker process

- **Decision:** Every Dramatiq queue is consumed by exactly one worker process. Processes, thread counts, queues and actor modules are declared in `workers/specs.py`. Workers start with `python -m kalkan.workers <name>` and an explicit queue list, and a test checks the mapping. Other processes send to an actor by name.
- **Why:** Dramatiq consumes by queue. A message for an actor that is not loaded in the consuming process is dropped with `ActorNotFound`.
- **Alternatives:** A broker module per process and hand-written queue lists.

### K31 · CI starts its infrastructure with docker compose

- **Decision:** The integration tests in CI start Postgres, Redis and the other services with the local compose file, not with GitHub service containers.
- **Why:** The same Postgres init script (roles) runs as on a developer machine. Service containers cannot take the repository's scripts or fake-gcs's command arguments.
- **Alternatives:** Service containers and a separate role setup step.

### K32 · Dependabot uses the uv ecosystem

- **Decision:** Dependabot watches Python through the `uv` ecosystem and the compose files through `docker-compose`.
- **Why:** The `pip` ecosystem does not update `uv.lock`, and image tags in the compose files should stay current too.
- **Alternatives:** The `pip` ecosystem.

### K33 · Library pins and their consequences

- **Decision:** APScheduler is pinned below 4. On Dramatiq 2, the Prometheus middleware is added explicitly and only in worker processes. Starlette's test client uses `httpx2`.
- **Why:** APScheduler 4 has a different API. Dramatiq 2 removed Prometheus from the default middleware and creates its metrics at worker start. Starlette dropped `httpx`.
- **Alternatives:** APScheduler 4; adding Prometheus in every process.

### K34 · Mailpit's SMTP on host port 2525

- **Decision:** Locally, Mailpit's SMTP port is published on the host as 2525. Inside the network it stays 1025.
- **Why:** Port 1025 is often taken on developer machines, for example by mail bridges.
- **Alternatives:** Host port 1025.

### K35 · No secrets in logs, error reports or CI

- **Decision:**
  - Tracebacks in the logs are written without local variables, and uvicorn's duplicate record of an already handled error is dropped.
  - Sentry gets no local variables and no request bodies. `X-Edge-Auth` and the session cookie are scrubbed, and `/e/<token>` paths are masked.
  - GitHub Actions are pinned by commit SHA, and CI runs gitleaks over the full history.
- **Why:** Local variables carry request headers, cookies and secrets (§23, §24). Several actions no longer publish moving major-version tags.
- **Alternatives:** structlog and Sentry defaults.

### K36 · No automatic major upgrades

- **Decision:** Dependabot proposes only patch and minor updates (GitHub Actions excepted). Major upgrades, such as Postgres 18, Redis 8, Python 3.13 or APScheduler 4, are made by hand and recorded here.
- **Why:** Postgres 18 changes the data directory layout, Redis 8 has a different license, and the project targets Python 3.12.
- **Alternatives:** Closing major-version PRs one by one.

### K37 · The origin server behind a proxy

- **Decision:** The real server behind a proxy such as Cloudflare is stored in the `domains.origin_*` fields, with `origin_source` set to `cloudflare_disclosure` or `operator`. `hosting_org` is the owner of the resolved IP, so it is Cloudflare for a proxied site.
- **Why:** The panel shows both values: the Hosting column shows the proxy and the Infrastructure column the origin. The channel plan uses the hosting provider only when the origin is known.
- **Alternatives:** Case-level fields.

### K39 · `cases.last_action` is short English text

- **Decision:** `cases.last_action` holds short English text such as "Host notified" or "Notice being prepared". The panel adds the time from `last_action_at`. Watching rows show the next check from `next_check_at`.
- **Why:** It feeds the design's "Last action" column directly.
- **Alternatives:** A coded enum and a translation table.

### K40 · Case-level signals have no scan

- **Decision:** `signals.scan_id` is null for case-level signals such as `brand_in_domain` or `domain_age_days`. The unique key `(case_id, scan_id, name)` uses `NULLS NOT DISTINCT`.
- **Why:** These signals do not come from a scan, and the upsert must stay idempotent.
- **Alternatives:** A fake scan for every signal.

### K41 · Task kinds

- **Decision:** `review_tasks.kind` includes `bounce` and `reply`. `case_id` may be null, and `notice_id` links a task to a notice (for example a manual-channel task).
- **Why:** The follow-up stage opens these tasks, and a manual channel needs to point at its notice.
- **Alternatives:** Separate task tables.
- **Status:** The current task kinds are listed in `domain/enums.py`.

### K42 · Blocking is recorded per notice

- **Decision:** A notice has a `blocked` status and a `blocked_reason`. While the case is `approved`, `cases.blocked_reason` repeats the reason as a summary.
- **Why:** The gates apply per notice. The channel limit and the lawyer approval depend on the channel and the template.
- **Alternatives:** A case-level reason only.

### K43 · Default daily channel limits

- **Decision:** `settings.channel_daily_limits` defaults to hosting 50, registrar 20 and google 50 real notices per UTC day. A `cloudflare` entry (50) remains from the original plan but is unused (K56), and registry gets 20 (K136). The initial migration writes the default settings.
- **Why:** No volumes were known yet; they are small at first, and registrar reputation is sensitive.
- **Alternatives:** No limits.

### K44 · Bucket per prefix

- **Decision:**
  - The evidence bucket holds `cases/`, `messages/`, `brands/` and `customers/`.
  - The reports bucket holds `reports/`, `eval/` and `demo/`.
  - The deletion rule applies only to `cases/` and `messages/`.
- **Why:** Logos and letters of authorization (LOA) must not expire on day 365.
- **Alternatives:** A separate assets bucket.

### K45 · `users.display_name`

- **Decision:** Users have a display name.
- **Why:** The design shows an account name (for example "Mintera Security").
- **Alternatives:** Derive it from the e-mail address.

### K46 · The demo workspace is frozen

- **Decision:** The demo customer's cases never enter the pipeline. The scheduler, the reconciler, follow-up, the outbox dispatcher and `admit_match` skip `customers.is_demo`. Permutations, enrichment and re-checks are not produced for it. The seed writes realistic `next_check_at` values for display.
- **Why:** Demo data must not enter the real pipeline, and nothing may be sent to its fictional domains.
- **Alternatives:** Leaving `next_check_at` empty.

### K47 · RLS expression and context

- **Decision:**
  - The policy expression is `NULLIF(current_setting('app.customer_id', true), '')::uuid`, and the context is set with `set_config(..., true)` per transaction.
  - `EXECUTE` on the resolver functions is revoked from `PUBLIC`.
  - `auth_recent_failures` returns the e-mail and IP failure counts separately.
- **Why:** A pooled connection keeps the setting as an empty string, and a direct cast to uuid would fail. `SET LOCAL` takes no parameters.
- **Alternatives:** `SET LOCAL` with string concatenation.

### K48 · Event types

- **Decision:** `case_events.type` includes `decision` and `note`. The event of a transition is chosen from the (from, to) states, and a dismissal can be recorded as `whitelisted`.
- **Why:** §14 requires every decision as an event, and §9 requires a note when RDAP is unavailable.
- **Alternatives:** Free-text event types.

### K49 · The demo seed reproduces the design exactly

- **Decision:** The demo seed produces the design's charts and KPIs exactly: 128 detected, 47 verified and 39 taken down, plus the previous period's numbers.
  - The design's six "Recent threats" rows exist as cases, but they may not be the newest rows in a newest-first table, because the design contradicts its own chart.
  - To keep the chart, five cases closed in the last three days are written `taken_down` before 72 hours have passed.
  - Dates are shifted to the day the seed runs. A run before 10:30 Warsaw time moves today's events back in absolute time; totals never change.
- **Why:** The panel's tests compare the screens with the design's numbers.
- **Alternatives:** Breaking the chart to make the table rows newest.

### K50 · A local `TOTP_ENCRYPTION_KEY` in `.env.example`

- **Decision:** `.env.example` ships a TOTP encryption key that is valid for local use only.
- **Why:** `make seed` and `make demo` must work on a clean clone without generating a key by hand. Self-hosted servers generate their own key (`deploy/selfhost/init-env.sh`).
- **Alternatives:** A key generation step on every install.

### K51 · UUID v7 from `uuid-utils`

- **Decision:** Ids are UUID v7, generated in the application with `uuid_utils.compat.uuid7`.
- **Why:** Neither the Python 3.12 standard library nor PostgreSQL 16 has UUID v7.
- **Alternatives:** The `uuid6` package.

### K52 · A separate test database, migrated from scratch

- **Decision:** Integration tests run against a separate `kalkan_test` database, migrated `downgrade base` and `upgrade head` at the start of every session. They use separate test buckets (`kalkan-test-evidence`, `kalkan-test-reports`) and empty them at the end.
- **Why:** Development data is not touched, and every run proves the migrations.
- **Alternatives:** Running tests inside transactions in the development database.

### K53 · One set of operator functions

- **Decision:** Operator actions live in `admin.py` and are shared by the CLI and the ops panel. Every action writes `audit_log`.
  - Adding an official domain also adds an `official` whitelist entry.
  - A whitelist CSV import with any invalid row imports nothing.
  - Brand keywords are at least four characters long. Keywords and slugs derived from a name are folded to ASCII (Ş → s, ł → l, ı → i).
- **Why:** One behavior and one audit trail.
- **Alternatives:** UI-specific logic.

### K54 · IDNA 2008 with UTS 46

- **Decision:** Host names are converted to punycode with IDNA 2008 and UTS 46 mapping (non-transitional), using the `idna` package.
- **Why:** Registries and CT logs use IDNA 2008. Python's built-in codec is IDNA 2003 and, for example, turns ß into ss.
- **Alternatives:** The built-in codec.

### K55 · Threat model: phishing clones and lure pages

- **Decision:** Kalkan handles two kinds of abuse:
  - **Live phishing clones** (`phishing_clone`) mirror the official site, pass the victim's credentials and one-time codes on to the real site, and take over the account.
  - **Lure pages** (`brand_impersonation`) pose as the brand's official or "verify your account" page, and their buttons send visitors to a clone or a third-party site.

  Copyright and content copying are out of scope, so `suggested_basis` is `phishing`, `trademark` or `none`.
- **Why:** These are the two kinds of abuse the target customers face.
- **Alternatives:** General brand protection including copyright (DMCA, DSA).

### K56 · The channel plan follows the notice's basis

- **Decision:** The channels depend on the basis of the notice:
  - **Phishing:** on day one, the registrar and the hosting provider (unless the site is behind a proxy); after 48 hours the registry; after 96 hours an operator task.
  - **Trademark:** the hosting provider, then an operator task after 48 hours. Whether to file a URS or UDRP complaint is decided with the customer.

  Cloudflare's abuse channel is never used. The `cloudflare` channel value, the `cloudflare_disclosure` origin source and the `dmca` and `dsa16` template values stay in the schema for old rows but are never planned. The escalation event keeps the name `registrar_escalated`; its data carries the level and the channel.
- **Why:** The goal is to take the site down. Cloudflare does not take sites down: it shows a warning and forwards the complaint. Under ICANN's DNS abuse obligations (since April 2024), registrars and registries must act on phishing, and a suspension works even when the site is behind a proxy. Registrars rarely act on trademark complaints.
- **Alternatives:** A Cloudflare report and origin disclosure; the registrar only after 48 hours (K12).

### K57 · Three protections against false positives and hidden kits

- **Decision:**
  1. A domain that shares an IP address or name server with an official domain gets the `official_infrastructure` signal, is never notified automatically, and goes to review. It may be the customer's own new domain.
  2. A look-alike domain that redirects to the official site is not dismissed for that alone. It is watched (rule R5) and checked daily for seven days from the target market with the mobile profile.
  3. Scans collect button and link targets (`cta_targets`). An unrecognized target of a lure page becomes a new candidate.
- **Why:** Brands that rotate domains to get around blocking change official domains often, and a complaint must never hit the customer's own site. Live phishing kits hide by redirecting scanners to the real site. The real threat of a lure page is where its button leads.
- **Alternatives:** Trusting the whitelist alone; treating a redirect to the official site as a dismissal.

### K58 · Matcher scoring

- **Decision:**
  - **Scores:** the keyword as the whole registered label scores 100; inside the registered label 90; only in a subdomain, or spilling over into the public suffix, 80.
  - **Homoglyphs:** a match found only after folding counts as `homoglyph`. It counts only when it finds the brand in a better place than the plain keyword does.
  - **Permutations** score 90.
  - **Fuzzy matches** (rapidfuzz ratio of at least 85, a folded registered label of at least five characters, and no keyword in the label) score the ratio minus 10.
  - **Extra reasons** add 5 each, up to 100.

  All brands compile into one matcher. `domains.name` is the full host (with `*.` and `www.` removed). `registered_domain` is stored separately (the PSL's private section included), and `tld` is the rightmost label.
- **Why:** The score is explainable and lines up with the threshold of 80 in §10. Phishing in a subdomain (`login.mintera.com.evil.xyz`) must be scanned on its real host.
- **Alternatives:** One automaton per brand; using the registered domain as the candidate.

### K59 · Discovery parameters

- **Decision:**
  - **Fuzzers:** dnstwist runs the fuzzers listed in §8 plus tld-swap, but not the cyrillic fuzzer, because the matcher's folding catches Cyrillic homoglyphs. Each domain is credited to the first fuzzer in `FUZZERS` order.
  - **Splitting:** official domains are split with the PSL, not dnstwist's list, and subdomains are dropped (`mintera.com.br`, `m.mintera.com`). Market words and TLDs live in `discovery/permutations.py`.
  - **Permutations** are generated when an official domain is added (in the same transaction) and weekly. Stale ones are deleted, and the brand row is locked meanwhile. Demo and suspended customers get none.
  - **DNS check:** NS, or A when there is no NS. NXDOMAIN means unregistered; a timeout means unknown and is not written. 20 concurrent lookups, 50 per second, in chunks of 500.
  - **CZDS** runs at 06:30 UTC. An empty `CZDS_TLDS` means every approved zone except com and net (K106). One download per zone per day is marked in Redis (36 hours).
  - **Scheduler:** it keeps each job's last send in Redis (`scheduler:last_sent`) and sends a missed run once at start-up. The first start only records.
  - **Manual candidates** score 100; an existing match is raised to 100 with a `manual` reason. URLs with backslashes, spaces or user info are refused.
- **Why:** Approved TLDs change over time, so the list should not be kept by hand, and com and net are very large. The DNS provider must not be flooded, and tests must be deterministic.
- **Alternatives:** A fixed TLD list; every dnstwist fuzzer.

### K60 · CT consumer

- **Decision:**
  - **Feed:** the consumer reads certstream's lite feed.
  - **"Seen" check:** it runs after matching and only for matched hosts (`ct:seen:<host>`, 24 hours). The key is set before writing and deleted if the write fails.
  - **Reconnects:** the back-off resets when a connection delivers its first message.
  - **Matcher cache:** it detects changes by a hash of the rows. Without a database it keeps the old matcher, and without any matcher it retries every 5 seconds.
  - **Profile:** certstream and ct-consumer are in the `ct` compose profile (`make up-ct`), not in `make up`.
  - **Health and metrics:** metrics are on port 9300. The health check is a heartbeat file that is not updated if no message was processed in the last 120 seconds. Discovery metrics are created on first use.
- **Why:** The CT feed carries thousands of certificates per second (about 2,000 per second in a local trial). Writing every host to Redis would mean tens of millions of keys a day, while the matcher handles about 65,000 domains per second. A local stack should not connect to the real CT logs by itself.
- **Alternatives:** The "seen" check before matching; the domains-only feed.

### K61 · Vendored PSL and confusables; httpx2

- **Decision:**
  - The Public Suffix List and Unicode `confusables.txt` (UTS #39, version 18) are kept byte-for-byte in `backend/src/kalkan/discovery/data/` and are exempt from the whitespace hooks.
  - tldextract reads the PSL file directly through a subclass, with no network, no cache and no `requests-file`.
  - The runtime HTTP client is `httpx2`, and its tests use `httpx2.MockTransport`.
- **Why:** Offline and reproducible. `requests-file` left the file open (ResourceWarning), and respx only mocks `httpx`, not `httpx2` (K33).
- **Alternatives:** tldextract's bundled snapshot; respx.

### K62 · Admission is queued after commit

- **Decision:** `admit_match` is queued right after the match is committed. If Redis is unreachable at that moment, the match is not queued; `brand_matches` records when admission checked the whitelist, and the reconciler re-queues matches that were never checked.
- **Why:** Queuing before commit would send a rolled-back match to a worker.
- **Alternatives:** Routing it through the outbox from the start.

### K63 · ASN from Team Cymru

- **Decision:** An IP's ASN comes from Team Cymru's IP-to-ASN DNS service (`origin.asn.cymru.com`, `origin6` for IPv6).
  - When the RDAP answer has no country code (ARIN), the hosting country comes from there too.
  - When the IP's RDAP record has no abuse address, the ASN's RDAP record (autnum) is the fallback.
  - A failed lookup retries the job and never keeps a stale ASN. A domain without a public IP gets empty ASN and hosting fields.
- **Why:** RDAP IP answers carry no ASN, and the `channel_contacts` host overrides are keyed by ASN. An override tied to an old ASN must not overwrite a new host's address.
- **Alternatives:** A paid IP database.

### K64 · What counts as shared official infrastructure

- **Decision:** `official_infrastructure` counts only infrastructure that is not shared with others.
  - IPs in Cloudflare's ranges and `*.ns.cloudflare.com` never count.
  - A name server counts only when it is under an official domain's own registered domain (vanity NS).
  - The value is true, false, or null when unknown (Cloudflare's ranges could not be fetched, or an official domain's DNS could not be read and no stored copy exists). The decision rules treat null like true, and the job is retried.
- **Why:** Cloudflare's anycast IPs and registrars' default DNS are shared by millions of sites. Treating "unknown" as "not official" could send a notice to the customer's own new domain.
- **Alternatives:** Counting every shared IP and NS; treating unknown as false.

### K65 · RDAP details

- **Decision:**
  - **Client:** whoisit runs with `follow_related=False` on its own `requests` session, with no retries inside a request; a 429 means "throttled" and the job retries later.
  - **Redirects and timeout:** only to the RIRs' https RDAP servers, with a 6-second timeout.
  - **Bootstrap data:** shared in Redis (kept 7 days, refreshed daily). A new copy downloads to a separate key, and a failed download keeps the old one.
  - **Rate limit:** a Redis token bucket per RDAP server (2 per second, burst 5, at most a 10-second wait).
  - **Cache:** IP and ASN answers are cached for 7 days. Non-public IPs are never queried.
  - **Untrusted data:** e-mail addresses from RDAP are cleaned. Addresses with control characters, spaces, more than one `@`, or more than 254 characters are dropped.
  - **Writes:** evidence is written only to open cases. The `rdap_unavailable` note is written once per case.
- **Why:** The registrar's RDAP server comes from the answer, not from the bootstrap (§10), and the redirect rule must not be bypassed. IANA's and the RIRs' servers must not be flooded. RDAP data is untrusted.
- **Alternatives:** Following the registrar's RDAP link; a bootstrap copy per worker.

### K66 · Outbox dispatcher and reconciler basics

- **Decision:**
  - **New cases** start with `last_action` "Detected". A demo customer's match is marked and left.
  - **Official domains:** adding one creates its domain row and queues `enrich_domain` and `whitelist_changed` through the outbox. A CLI whitelist import also leaves `whitelist_changed`.
  - **Dispatcher:** it claims rows with `SKIP LOCKED` and marks them after sending, so a job goes at least once and every job is idempotent. A broker (Redis) failure does not use up an attempt. A broken row (unknown kind, bad payload) does, and after 10 attempts it is left for the ops panel. Demo jobs are marked without being sent.
  - **Reconciler:** it re-queues a `detected` case with no HTTP scan, unless it has an open review task or a `job_failed` that stopped it (an `enrich_domain` failure does not stop it). It also re-queues a match that has not been checked for 10 minutes.
  - **Leaving `in_review`** closes the case's open review task.
- **Why:** These implement §7 and §9, and the reconciler is the counterpart of K62.
- **Alternatives:** Leaving re-queues to each job.
- **Status:** The first reconciler rule was changed by K74.

### K67 · Admission always enriches

- **Decision:**
  - **Queueing:** `admit_match` always queues `enrich_domain`, on redelivery too, while the case is still `detected`.
  - **Fresh domains:** `enrich_domain` makes no lookups for a domain enriched in the last 24 hours. It fills missing evidence and signals of open cases from the last answers, kept in Redis for 24 hours, and then starts the pre-check.
  - **Writing:** lookups and the official-domain footprints run first. A short write transaction then locks the domain row `FOR NO KEY UPDATE`, and evidence already written in the same second is skipped.
  - **Limits:** the time limit is 120 seconds. On the last attempt every error (timeouts included) is written as `job_failed`, and `detected` cases continue to the pre-check.
- **Why:** The original "fresh domain goes straight to the pre-check" left new cases without evidence and signals, which switched off K57's protection. In the worst case DNS, Team Cymru, three RDAP lookups and Web Risk do not fit in 60 seconds.
- **Alternatives:** A direct pre-check for fresh domains; one long transaction.

### K68 · Contact overrides

- **Decision:** The e-mail of a `channel_contacts` override is written to the domain fields during enrichment. The web form URL and the language are read from `channel_contacts` when notices are planned. Override keys are canonical numbers (`AS024940` becomes `24940`).
- **Why:** An override added later reaches the next notice without waiting 24 hours for re-enrichment.
- **Alternatives:** Re-enriching domains when an override is added.

### K69 · The SSRF-safe HTTP client

- **Decision:** `net/safe_http.py`, on top of httpx2:
  - **Address checks:** it resolves DNS itself (getaddrinfo) and refuses the request unless every address is public (`is_global`, with IPv4-mapped IPv6 unwrapped).
  - **Connection:** it connects to the IP in the URL, and the `Host` header, TLS SNI and certificate check use the real host name.
  - **No shared state:** no connection pool and no cookie jar. A site's cookies go back only to the same host within the same fetch.
  - **Redirects:** followed by hand, and each step is resolved and checked again.
  - **Refused input:** URLs with credentials, schemes other than http and https, and environment proxy settings (`trust_env=False`).
  - **Body limits:** the body is read as raw bytes, and the 2 MB limit applies to the network bytes. Only gzip and deflate are accepted and decompressed in the client, with the output also capped at 2 MB.
  - **Time limits:** 5 seconds per read and 15 seconds in total, or a shared deadline from the caller. A server that trickles header bytes is cut off by the job's own time limit.
  - **Errors:** every error becomes `FetchError`.
  - **Proxy:** the pre-check goes out directly, without a proxy.
  - **Tests:** a port mapping applies only while `allow_cidrs` is set (fixtures listen on host port 18080). Special-use TLDs (`test`, `example`, `invalid`, `localhost`) are added to the PSL splitter as suffixes.
  - **HTML parsing:** stdlib `html.parser`, read the way browsers do. This covers the BOM and browser-only charsets, an implicit `</head>`, the first `<base>`, backslashes, frames and meta refresh without `url=`. NUL characters are dropped, lists and stacks are bounded, and the work is linear in the page size.
- **Why:**
  - An IP in the URL closes DNS rebinding without changing httpx2's transport.
  - A pooled TLS session could skip the certificate check for another host on the same IP, and shared cookies would leak between sites.
  - Compressed bodies must not get around the size and time limits.
  - The proxy belongs to the browser stage, and the pre-check must stay cheap.
  - Pages are read without running them and without extra dependencies, so a hostile page cannot crash the worker.
- **Alternatives:** A custom transport or connection hook; BeautifulSoup or lxml; routing the pre-check through the proxy.

### K70 · A logging skeleton for `browser_capture`

- **Decision:** Before the browser stage existed, `browser_capture(case_id)` was a logging skeleton in worker-default consuming the `capture` queue.
- **Why:** The pre-check could already queue its stage-2 decisions without messages piling up.
- **Alternatives:** A queue without a consumer.
- **Status:** Replaced by K90 and K99. Old skeleton messages (a bare case id) are logged and dropped.

### K71 · HTTP pre-check details

- **Decision:**
  - **URLs tried, in order:** the manual candidate's URL (same host or `www.`; `http://` is added when the scheme is missing, and user info and fragments are removed), then `https://host/`, then `http://host/`, and for a registered domain finally `www.host`, because discovery drops `www.`. A host without DNS, or one the SSRF guard refuses, is not tried again.
  - **Budget:** all requests of one pre-check share 30 seconds (the actor limit is 45).
  - **Rule order:**
    1. no connection;
    2. redirect to the official site: watch and check again in one day (a meta refresh, JS or frame redirect on an almost empty page counts too);
    3. parked, unless there is a password field or a favicon match;
    4. bot challenge: send to stage 2;
    5. empty page, unless it redirects to a look-alike domain;
    6. markers: favicon match, the brand in the title or text, a password field, plain HTTP, or a redirect by meta refresh, JS or frame to another look-alike domain;
    7. a young domain with a strong match;
    8. otherwise watch, and check again in seven days.
  - **Brand detection:** spaces are removed from the text and the matcher's folding is applied. Keywords shorter than four letters do not count. The site's own host names, which error and placeholder pages print, are removed from the text first. Subdomains of the same registered domain count as the same site.
  - **Favicon:** the first two `<link rel=icon>` entries, then `/favicon.ico`. A non-image answer (HTML) does not count, and no favicon is fetched on a redirect to the official site.
  - **Targets that become candidates (at most 10):** every non-official target the matcher ties to the brand. If the page poses as the brand (brand in title or text, or a favicon match) and does not go to the official site, also its buttons, forms, redirects and frames, and links whose text is a call to action (sign in, log in, register…) or names the brand. Such a candidate is recorded like a manual one: source `manual`, score 100, reasons `manual` plus the matcher's, and details `via=cta`, the source case number, the cleaned URL and the button text.
  - **Once per stay:** a case is pre-checked once per stay in `detected`; the job is skipped if an HTTP scan exists since the last `detected` event. On the last attempt every error is written as `job_failed` (`job=http_precheck`). Demo cases and cases not in `detected` are skipped.
- **Why:** The parking and empty-page rules must not hide a redirect to the official site, a login form, or a doorway page's target. A lure page's real target is where its button leads, often a domain that does not mention the brand (K57). Share and social links must not become candidates, and the reconciler and redeliveries must not duplicate scans.
- **Alternatives:** A separate `cta` source type; promoting only look-alike targets; promoting every link.

### K72 · Official favicons

- **Decision:**
  - **When:** official favicons are fetched by the outbox job `refresh_brand_favicons(brand_id, domain)` when an official domain is added (demo excluded), and by `kalkan brand favicons`. Without a domain, the job queues one message per official domain (60 seconds per message, a 30-second request budget per domain).
  - **How:** with the same SSRF-safe client, https first and then http, `<link rel=icon>` first and then `/favicon.ico`. The file type comes from the first bytes (ico, png, gif, jpeg, bmp, webp, avif, svg), and the candidate side uses the same check.
  - **Storage:** the asset id is derived from the brand and the SHA-256 (uuid5). The brand row is locked while recording and the SHA-256 is checked again, so an object uploaded by an interrupted attempt is reused.
  - **Errors:** the command exits 1 for an unknown brand or a storage error.
- **Why:** The pre-check's favicon match needs references, and a new brand may have no official domain yet. A brand that rotates domains can have dozens of them, more than one job fits in 120 seconds. Concurrent jobs must not store the same icon twice or leave orphaned objects.
- **Alternatives:** A weekly scheduled job; one job for all domains.

### K73 · Fixed local network and private addresses

- **Decision:** The local compose network uses a fixed subnet (172.30.0.0/16), and `fixture-sites` sits at 172.30.255.80 with `*.test` aliases. The `official_infrastructure` check does not treat shared loopback, RFC 1918, CGNAT, link-local or unspecified addresses as shared infrastructure.
- **Why:** worker-default's `SSRF_ALLOW_CIDRS` can then allow a single address. All fixture sites share one private IP, and the clone must not count as official infrastructure in the end-to-end test. Sharing a private address (such as a 127.0.0.1 sinkhole) does not mean sharing a server.
- **Alternatives:** A container per fixture.

### K74 · Narrower parking and challenge patterns

- **Decision:**
  - **Parking:** generic parking phrases ("related searches", "account suspended") count only in the title or on pages shorter than 1,500 characters.
  - **Challenges:** only interstitial signatures count: Cloudflare's "Just a moment", `_cf_chl_opt`, challenge `orchestrate` paths, `cf-mitigated: challenge`, and the DDoS-Guard header. Cloudflare's JavaScript Detections script (`/cdn-cgi/challenge-platform/scripts/jsd/`), which it adds to every page, does not count. reCAPTCHA, hCaptcha and Turnstile count only on a captcha wall (under 500 characters of visible text and no password field).
  - **Reconciler:** its first rule changed from "no HTTP scan for 2 hours" to "no HTTP scan since entering `detected`". A case sent to stage 2 is not pre-checked again, and a stuck browser job is caught by K101.
- **Why:** Every Cloudflare site with Bot Fight Mode, and every clone with a captcha on its login form, counted as a bot challenge, and FAQ pages counted as parked. The old rule rescanned stage-2 cases every two hours and duplicated scans, signals and browser jobs.
- **Alternatives:** Broad patterns; a separate case state.

### K75 · One Python patch version everywhere

- **Decision:** Local development and CI use the same Python patch as the production image (`backend/.python-version` and the Dockerfile's `python:3.12.x`). The two change together.
- **Why:** `html.parser` changed behavior between patch releases: 3.12.0 parsed tags inside `<title>`, while later patches treat them as text, as browsers do. Tests must measure production behavior.
- **Alternatives:** Any 3.12 on the machine.

### K76 · Deploy gate (reference deployment)

- **Decision:** Workload Identity Federation (`infra/terraform/wif.tf`) issues Google credentials only to the first attempt of a manual `workflow_dispatch` run of one named deploy workflow, on `main`, in one repository, started by one deployer. The repository and deployer are matched by their numeric GitHub ids, and `workflow_ref` is compared exactly. `kalkan-ci` can be impersonated only by that repository's principal set. A repeated deploy is a new run, never "Re-run".
- **Why:** Starting the run by hand is the approval. The WIF condition stops any other branch, event, workflow or user from obtaining credentials. A re-run keeps the first attempt's `actor_id` (the OIDC token has no `triggering_actor`), so anyone with write access could replay an old deploy with its old migrations; `run_attempt` closes that. Names can be re-registered, numeric ids cannot.
- **Alternatives:** GitHub environment approvals (not available on every plan); automatic deploys on merge; conditions on repository names.
- **Status:** The repository ships no deploy workflow (K187). The Terraform condition stays for anyone who adds one.

### K77 · GCP naming and network (reference deployment)

- **Decision:**
  - **Naming and network:** service accounts `kalkan-api`, `kalkan-web`, `kalkan-vm` and `kalkan-ci`. A dedicated VPC `kalkan` with one subnet (10.10.0.0/24, Private Google Access) and a single ingress rule for IAP SSH. The project's default network is not used.
  - **APIs:** the extra APIs needed by WIF, Terraform and OS Login are enabled by `bootstrap.sh` before the first apply.
  - **Cloud SQL:** `ENTERPRISE` edition, connector enforcement `REQUIRED`, and deletion protection in Terraform and at the API.
- **Why:** Short names like `sa-vm` are below GCP's six-character minimum. The default network's rules open SSH, RDP and ICMP to the world. Enabling APIs takes effect with a delay. The PostgreSQL 16 default (Enterprise Plus) does not allow shared-core tiers. Connector enforcement opens the public IP only to the Auth Proxy and the Cloud Run connector.
- **Alternatives:** Longer `sa-*` names; deleting the default network; a private IP with a VPC connector (extra cost).

### K78 · Generated database and Redis passwords (reference deployment)

- **Decision:**
  - **Generation:** Terraform generates the passwords of the three database roles and the two Redis ACL users as `ephemeral` random values. It writes them with write-only arguments (`password_wo`, `secret_data_wo`; Terraform 1.11+), together with the `DATABASE_URL_*` and `REDIS_URL` secrets that embed them.
  - **Rotation:** bump `db_password_version` or `redis_password_version`, apply, then deploy. The two groups never touch each other, and old versions are disabled, not destroyed.
  - **Other secrets:** the edge secret, the TOTP key and the Grafana password are generated by a documented command. Provider keys get empty containers.
  - **Loading:** the VM's loader skips optional secrets without an enabled version, but stops without the Redis passwords. Cloud Run binds an optional secret only when its newest version is enabled.
- **Why:** Passwords never reach the plan, the state, a file or a shell history. A URL and the role's password are written in the same apply. Separate version variables mean a database rotation does not restart Redis (and the queue).
- **Alternatives:** `random_password` (stored in state); passwords typed by the operator; placeholder values in empty containers.

### K79 · Cloud Run api (reference deployment)

- **Decision:**
  - **Split:** Terraform creates the `api` service with a placeholder image and only manages scaling (0 to 2). The image, port, memory, CPU, environment, secrets and Cloud SQL attachment belong to the deploy, and Terraform ignores them.
  - **Logs:** Cloud Run's own request logs drop `/e/<token>` URLs through a log exclusion.
  - **Access:** the service is public (`invoker_iam_disabled`), and `X-Edge-Auth` protects it.
  - **Settings:** shared between Cloud Run and the VM from the same deploy values. Cloud Run gets no `REDIS_URL` (K3).
- **Why:** The service can exist before an image does, and `plan` does not revert deploys. The Worker calls run.app without a Google identity. Evidence links are 90-day bearer tokens, and project log readers would otherwise see them (K35).
- **Alternatives:** Letting the deploy create the service; keeping the environment in Terraform; calling with a Google identity.

### K80 · Health alias `/api/health`

- **Decision:** `GET /api/health` is an alias of `/api/healthz`. It is listed in the API's health paths and needs no edge header. Uptime checks and smoke tests use it. The edge Worker forwards `/healthz` and `/api/healthz` on either host to `/api/health`.
- **Why:** Cloud Run's front end answers some paths ending in `z` (`/healthz`) itself.
- **Alternatives:** Renaming the existing health endpoints.

### K81 · VM delivery (reference deployment)

- **Decision:**
  - **Startup:** an idempotent script (`infra/vm/startup.sh`) runs on every boot. It installs pinned Docker Engine, containerd and the compose plugin from Docker's apt repository (with a key fingerprint check and `apt-mark hold`), plus the gcloud CLI and Artifact Registry auth.
  - **Bundle:** the compose file, `vm/`, the SQL bootstrap and the env files are built from the deployed commit and copied over IAP SSH.
  - **`deploy.sh`:**
    - validates the bundle (required files, `ENV=prod`, no test settings or secrets in `app.env`);
    - copies it with `rsync --inplace` and installs the systemd units;
    - loads the firewall and the secrets, and pulls the images;
    - runs db-bootstrap and the migrations;
    - waits with `up --wait`.

    Concurrent deploys are locked out.
  - **Secrets:** they live in `/run/kalkan/<service>.env` (tmpfs, 0600). Each service gets only its own secrets (`vm/secrets.map`), and multi-line values are refused.
  - **Firewall unit:** `kalkan-firewall.service` is a hard dependency of Docker (`RequiredBy=docker.service`), so Docker does not start without the rules.
  - **Passwords:** db-bootstrap passes them to psql through its environment (`\getenv`), never on the command line.
  - **Rollback:** deploy an older `image_tag` with migrations skipped. Schema rollback is manual ([operations.md](operations.md#rollback)).
- **Why:**
  - A compose file in instance metadata would need a Terraform apply for every change, and the bundle and images come from the same commit.
  - Secrets are not written to images or persistent disk.
  - A weak `WantedBy=` dependency would let Docker restart containers (the browser included) without the rules.
  - Process arguments are visible in `/proc/<pid>/cmdline`.
- **Alternatives:** Compose via metadata; one env file for all services; the application reading Secret Manager itself.

### K82 · Browser network firewall, metadata access and Redis users (reference deployment)

- **Decision:**
  - **Browser network:** the browser runs on its own bridge network (`kalkan-browser`, fixed bridge name and subnet), where Redis also has an address. `vm/bin/firewall.sh` writes its own iptables chains in one `iptables-restore --noflush`. Traffic from the bridge may get replies and reach Redis on 6379. Link-local, RFC 1918 and CGNAT ranges are rejected, and the VM itself sees only replies. The rules are IPv4 only (IPv6 is off on that network). DNS goes through Docker's embedded resolver, and no `dns:` option is set.
  - **Metadata server:** the default compose network has a fixed bridge and subnet. Only four fixed addresses (`cloud-sql-proxy`, `worker-default`, `worker-ai`, `ops`) may reach the metadata server, and Docker hands out dynamic addresses only from the upper half of the subnet. CLI commands run with `docker compose exec`.
  - **Redis users:** Redis requires a password. The loader writes an ACL file with SHA-256 password hashes only: `default` is off, `kalkan` serves the application containers, and `browser` serves worker-browser.
- **Why:** DOCKER-USER sees only forwarded traffic, so traffic to the bridge gateway needs INPUT rules, and the rules must exist before Docker starts containers. A `dns:` server would be queried from the container's namespace and hit the rules. A compromised Chromium with password-less Redis could run any command, inject jobs and delete rate-limit keys. The metadata token reads every secret of `kalkan-vm`, so third-party images must not reach it.
- **Alternatives:** Allowing the metadata server for DNS; open DNS servers on the browser network; nftables; password-less Redis behind network rules only; metadata access for every container.
- **Status:** The browser user's rule was narrowed by K102.

### K83 · Monitoring stack (reference deployment)

- **Decision:**
  - **Components:** Prometheus (30-second scrapes; 15 days or 2 GB), Alertmanager, Grafana (Prometheus as the only data source; analytics and sign-up off) and Uptime Kuma on the VM, plus a Cloud Monitoring uptime check on the api.
  - **Alertmanager mail:** it sends through any SMTP relay (`ALERT_SMTP_*`). Its configuration is generated by the secret loader, and it drops alerts when the relay is not configured.
  - **Access:** the UIs listen on the VM's 127.0.0.1 only and are reached through an IAP tunnel.
- **Why:** UIs with weak authentication must not face the internet. Alertmanager expands no environment variables and has no file option for the SMTP user name.
- **Alternatives:** Exposing the UIs behind a load balancer with IAP.
- **Status:** Rules and the exporter came later (K172, K173). Self-hosted servers reuse the same configuration in the `monitoring` profile.

### K84 · Edge Worker and browser image (reference deployment)

- **Decision:**
  - **Worker:** `kalkan-edge` (`infra/cloudflare`) serves `app.` and `evidence.` as Custom Domains, with `workers_dev` and preview URLs off. Origins are passed at deploy time, and `EDGE_SHARED_SECRET` is a Worker secret.
  - **Responses:** without the secret, an https origin or a web origin, the Worker answers 503. An unknown host gets 404, and `http://` gets a 308 to https. Client-sent `X-Edge-Auth`, `X-Forwarded-*`, `Forwarded` and `Host` are dropped.
  - **Proxying:** requests go out with `redirect: "manual"` and `cache: "no-store"`. A `Location` pointing at an origin host is rewritten to the public host, and other `Location` values are left alone. An unreachable origin gives 502 (no URL is logged). Invocation logs and traces are off.
  - **Tests and pins:** the tests run in workerd without network. Tool versions are pinned, and `sharp` is overridden to a fixed release.
  - **Browser image:** it is based on Microsoft's Playwright Python image. `pwuser` gets uid and gid 10001, like the backend's `kalkan` user. The `playwright` package and the image are upgraded together by hand with `profiles.CHROMIUM_MAJOR`.
- **Why:**
  - The Worker is the origin, so it needs a Custom Domain, not a route. URLs carry evidence tokens.
  - FastAPI's slash redirects build absolute URLs from the Host it sees (run.app). Behind Cloud Run, uvicorn also sees the scheme as `http`, so the browser would follow the redirect to run.app and get a 403.
  - Playwright's browsers must match the image, and the hand-off volume is shared by the same uid in both images.
- **Alternatives:** A route and DNS record; origins as secrets; leaving `Location` alone; `playwright install --with-deps` on the plain Python image.

### K85 · Optional services as compose profiles

- **Decision:** `ops`, `worker-ai` and `worker-browser` are compose profiles in the reference VM file, selected with `COMPOSE_PROFILES`. The deploy writes the value into the bundle's `.env`, so a hand edit on the VM is replaced by the next deploy. A profile that is turned off is stopped by hand (`docker compose rm -sf`). `migrate` and `psql` are one-off services in a `tools` profile. certstream runs with a client buffer of 3,000 and recovery off.
- **Why:** Services whose code did not exist yet would crash-loop. A profile turned on only on the VM would silently disappear on the next deploy, and `up --remove-orphans` does not treat services of disabled profiles as orphans. Recovery without a stored index would download every CT log from the start, and CZDS and permutations catch what was missed.
- **Alternatives:** Placeholder services; keeping profiles on the VM; recovery on.
- **Status:** The local and self-host compose files use profiles the same way (`browser`, `ai`, `ct`, `monitoring`, `demo`).

### K86 · IAM placement (reference deployment)

- **Decision:** Roles are granted on the narrowest resource.
  - **`kalkan-ci`:** Cloud Run developer and compute viewer at project level; IAP tunnel and OS admin login only on the VM; `serviceAccountUser` only on the runtime accounts; `secretmanager.viewer` only on the secrets bound to Cloud Run (versions, not values).
  - **Artifact Registry:** writes by `kalkan-ci` and reads by `kalkan-vm`, both only on the `kalkan` repository.
  - **`kalkan-vm`:** may create and read evidence objects but not delete them, and has object admin only on the reports bucket.
  - **`kalkan-api`:** reads both buckets.
  - **Web Risk:** called with an API key, without an IAM role.
- **Why:** OS Login on a VM with a service account needs actAs. The deploy must check whether an optional secret is ready without reading it. Evidence is deleted only by the lifecycle rule.
- **Alternatives:** Project-level roles and secret access; ADC and an IAM role for Web Risk.

### K87 · Terraform layout (reference deployment)

- **Decision:**
  - **Layout:** one root module with one file per service, as tflint's standard module structure expects.
  - **Provider:** `user_project_override` with `billing_project`, so quota is charged to the right project.
  - **Lock file:** committed for four platforms. CI and `make infra-check` use `-lockfile=readonly`.
  - **Cloud SQL:** encrypted connections only, zonal, nightly backups and a Sunday maintenance window. The disk grows automatically, and backups survive instance deletion. The database and owner role are abandoned, not dropped, if removed from Terraform.
  - **Protection:** the buckets have `prevent_destroy`.
- **Why:** The checks in Docker must not rewrite the lock file on Apple Silicon. A plan must not show surprise replacements. A role that owns objects cannot be dropped in Cloud SQL.
- **Alternatives:** Modules; generating the lock file in CI; PITR and high availability (cost).

### K88 · VM compose details (reference deployment)

- **Decision:**
  - **Hardening:** every image is pinned, and backend-image services run with `no-new-privileges`.
  - **Memory:** every service has a memory limit, and Redis runs `--maxmemory 192mb --maxmemory-policy noeviction` without persistence.
  - **worker-browser:** it gets no `app.env`, only `ENV`, `LOG_LEVEL` and its own secrets file (`REDIS_URL`, `PROXY_MAP`, `SENTRY_DSN`). It runs as `pwuser` with `init`, no capabilities, 2 GB and 1.5 CPUs, on the browser network only.
  - **psql tool:** connects as `kalkan_owner` through the local proxy, and its output does not go to the logs.
  - **Logs:** go to Cloud Logging.
- **Why:** The VM's 8 GB must fit Chromium. A full Redis should fail writes, not get killed. The browser must not even see operator e-mails or bucket names, and query output must not land in the log store.
- **Alternatives:** Unlimited services; Redis AOF; a shared `app.env` for the browser.

### K89 · CI pipeline

- **Decision:** `ci.yml` runs on every push to `main` and on pull requests:
  - lint, the OpenAPI schema check and gitleaks;
  - unit, integration and benchmark tests;
  - image builds for `backend` and `browser`;
  - Terraform `fmt`, `validate` and TFLint;
  - the edge Worker's typecheck and tests;
  - compose `config` checks of the VM and self-host files with every profile, Prometheus rules and Alertmanager template checks, and shellcheck.

  `make infra-check` runs the same tools from pinned Docker images. Dependabot covers Terraform and npm too, without major versions (K36).
- **Why:** Errors are caught without touching any cloud resource, and the jobs need no secrets.
- **Alternatives:** Manual checks.
- **Status:** The deploy half of the original pipeline is not shipped (K187).

### K90 · The browser job model

- **Decision:**
  - **Message:** `browser_capture(job: dict)` takes a `capture_contract.CaptureJob`: a UUID v7 `job_id`, a purpose (`case`, `reference` or `recheck`), at most five entry URLs, a country, the profiles and `issued_at`.
  - **Issuing:** only trusted processes issue jobs, through `issue_capture` in worker-default and the scheduler. The issuer pre-assigns a scan id per profile and stores a `CaptureRecord` (customer, brand, case, scan ids, URLs) in Redis for 50 hours. For case jobs it also sets a pending marker per case with `SET NX`; if a marker exists, no job is issued. A failed send removes the marker, and only the job itself removes it otherwise.
  - **URLs and country:** the entry URLs are K71's, led by the one that answered the pre-check. The country is the brand's first target market.
  - **Expiry:** jobs older than 48 hours are dropped by the browser, and the reconciler issues a new one.
- **Why:** The browser has no database, so the usual "actor takes only an id" rule cannot hold there. The browser is untrusted (K82), so ids must not come from it. Fixed scan ids keep storage paths stable across retries. Without the marker, the reconciler would issue a new job every 10 minutes, and self-rescheduling messages could fill Redis. Redis only carries jobs; a lost job is rebuilt from case state (§2).
- **Alternatives:** A `capture_jobs` table; giving the browser only a case id and asking worker-default for the rest; deduplication without a marker.

### K91 · Persisting a capture from an untrusted folder

- **Decision:** `persist_capture(job_id)` in worker-default takes only the job id from the browser's message. If the job record is gone, it deletes the folder and stops.
  - **Opening files:** the folder is opened with `O_DIRECTORY | O_NOFOLLOW`, and files relative to it with `O_NOFOLLOW | O_NONBLOCK`. Only `meta.json` and the names from `capture_contract.file_name(kind, profile)` are read.
  - **File checks:** each file must be a regular file with one link (`st_nlink == 1`) and under its size limit: viewport PNG 10 MB, full-page PNG 40 MB, HTML 15 MB, HAR 50 MB, `meta.json` 2 MB. Screenshots must carry the PNG signature.
  - **`meta.json`:** validated against a strict schema (`CaptureMeta`: no unknown fields, a length limit on every field). Its ids are informational only.
  - **Invalid folders:** not retried. The job writes `job_failed` (`invalid capture: …`) and sends the case to review (`capture_failed`).
  - **Existing objects:** a write that finds the object already there reuses it when the stored SHA-256 matches (K72).
  - **Configuration errors:** a missing bucket setting is raised on every attempt and never turned into `capture_failed`.
- **Why:** A compromised Chromium could ask for another tenant's case, plant symlinks or hard links to `/proc/self/environ`, or plant a FIFO that would block the worker. An unbounded `meta.json` could exhaust memory. Validation errors do not fix themselves on retry, and a configuration error is not the evidence's fault.
- **Alternatives:** Trusting the ids in `meta.json`; truncating large files; retrying validation errors.

### K92 · Capture outcome rules

- **Decision:**
  - **Per profile:** `ok` needs the viewport screenshot and the HTML without a navigation error. A failed full-page shot or inventory is only logged. A navigation timeout, a network error or a navigation that turns into a download is `error`. `challenge` overrides everything. A 4xx or 5xx page is still `ok`, with the code in `http_status`.
  - **Per job:**
    - If any profile is `challenge`, everything is stored, the case is recorded as `captured` and goes to review (`challenge`), and no signals are computed.
    - If every profile is `error` and it is not the last attempt, the browser deletes its partial folder and retries once.
    - On the last attempt the browser hands over with `final_attempt=true`, and persist writes the error scans, `job_failed` and `capture_failed`.
    - Success needs at least one `ok` or `challenge` profile and leads to `compute_signals`.
  - **State check:** everything is written only while the case is still `detected`.
- **Why:** One profile's failure does not invalidate the other's evidence. A challenge page goes to a human because CAPTCHAs are never solved (product.md §11). A 4xx page is also a state of the site. Retries stay inside the browser, and a hand-over happens only when there is something to store.
- **Alternatives:** Retrying every profile error; waiting or retrying on challenges; a `blocked` outcome.
- **Known gap:** a proxy's own error page for a dead domain on plain http, and national blocking pages, count as `ok`.

### K93 · Proxy URLs and the credential forwarder

- **Decision:**
  - **Format:** each `PROXY_MAP` value is an `http://[user[:password]@]host:port` template with an explicit port and nothing after it. `{session}` may appear only in the user name or password. It is replaced per attempt with `secrets.token_hex(4)`, and both profiles share it.
  - **Validation:** invalid values fail settings validation without echoing the input (`hide_input_in_errors=True`). A missing country means "no proxy" (K94).
  - **Country:** the job's country, else `PROXY_DEFAULT_COUNTRY`. Locale, time zone and `Accept-Language` follow the country actually used (`scanning/profiles.MARKETS`).
  - **Credentials:** Chromium never sees them. A per-attempt forwarder on 127.0.0.1 (`scanning/proxy_forwarder.py`) adds `Proxy-Authorization` upstream. HARs are scrubbed and dropped if they still contain credentials, and only the country and `host:port` are logged.
  - **WebRTC:** Chromium starts with `--force-webrtc-ip-handling-policy=disable_non_proxied_udp`.
- **Why:**
  - Playwright 1.63 answers every authentication challenge with the proxy credentials, a site's own 401 included, and Chromium writes `Proxy-Authorization` into the HAR stored as evidence.
  - Eight hex characters fit every major provider's session id format.
  - The same exit address for both profiles leaves the device as the only variable in the cloaking comparison.
  - Chromium cannot authenticate to SOCKS5, and an HTTP proxy does not carry UDP, so WebRTC would reveal the server's address.
- **Alternatives:** Giving Playwright the credentials; provider-specific fields; treating invalid values as "no proxy"; taking the locale from the target market.

### K94 · Deferring when there is no proxy or budget

- **Decision:** Capture fails closed (§7).
  - **No proxy:** no browser, context or page is opened. The job is re-sent as a new message 30 minutes later, without using an attempt (`capture_deferrals_total{reason="no_proxy"}`).
  - **Daily limit:** before each job the browser reads `kalkan:proxy:bytes:<YYYYMMDD>` in Redis. At or above `PROXY_DAILY_GB_LIMIT × 10⁹`, the job waits until the next UTC midnight plus 0 to 30 random minutes. A limit of 0 stops every job.
  - **Byte count:** after every attempt, the profiles' estimated bytes are added from the HAR (request headers and body, response transfer size).
  - **No Chromium:** if Chromium cannot start (for example, no sandbox), the job waits 30 minutes too and is never handed over as `capture_failed`.
- **Why:** Waiting is not an error (§7). Both browser processes share the counter. Providers bill both directions, and the HAR is the only size source the browser has. The random delay spreads the midnight peak.
- **Alternatives:** Counting CDP events; counting in worker-default; failing the case when the limit is hit.

### K95 · Browser runtime

- **Decision:** `scanning/capture.py` uses Playwright's sync API.
  - **Chromium:** one per process, started on first use with its sandbox (`chromium_sandbox=True`; never `--no-sandbox`, K104). It starts with an allow-listed environment, so `PROXY_MAP`, `REDIS_URL` and `SENTRY_DSN` never reach renderers, and with a sentinel proxy (`http://127.0.0.1:9`) at launch.
  - **Contexts:** each attempt opens a new context per profile. Desktop is 1280×800 at DPR 1 (Chrome on Windows). Mobile is 390×844 at DPR 3, `is_mobile`, `has_touch` (Chrome on Android). The UA major version comes from the real browser.
  - **Context options:** no downloads, service workers blocked, HTTPS errors ignored, media requests aborted, permissions denied, and dialogs and pop-ups closed.
  - **Navigation:** `domcontentloaded`, then `load` within the same 30 seconds, then at most 3 seconds of network quiet. At most 10 main-frame navigations per entry URL.
  - **Capture:** a viewport screenshot, scrolling up to 5 screens or 10,000 px, a full-page shot of up to 10,000 px, the HTML, the inventory and up to 20,000 characters of `innerText`.
  - **No stealth:** `navigator.webdriver` and similar properties are left alone.
- **Why:**
  - Sync API objects are bound to their thread.
  - Engine, UA and client hints must be consistent, because cloaking kits look for "Android" and "Mobile".
  - Service workers would bypass routing and the HAR.
  - A site with a broken certificate is evidence too.
  - Pages that never fire `load` must still be captured.
- **Alternatives:** `wait_until="load"`; a browser per job; waiting for full `networkidle`; DPR 1 on mobile.

### K96 · Time budget and the kill watchdog

- **Decision:**
  - **Budget:** an attempt has a shared 100-second budget (`JOB_BUDGET_SECONDS`), split evenly between the remaining profiles. No navigation or scroll step starts with less than 2 seconds left.
  - **Watchdog:** for calls without timeouts (`evaluate`, `content`), a two-stage watchdog SIGKILLs every Chromium process at budget + 8 seconds and the Playwright driver 5 seconds later.
  - **After a kill:** once the driver is gone, no Playwright call is made, and the browser restarts on the next job.
  - **Limits:** Dramatiq's 120-second time limit is the last resort, with `max_retries=1`. The browser restarts every 50 jobs.
- **Why:** After the driver dies, every sync Playwright call spins at 100% CPU forever, and Dramatiq's limit does not stop it (an 8-minute spin was observed). If Dramatiq's limit fires inside a Playwright call, it kills the dispatcher and the thread hangs. Killing Chromium first makes the pending call return with an error.
- **Alternatives:** Dramatiq's limit alone; a new browser per job; the async API with cancellation.

### K97 · Form and link inventory

- **Decision:**
  - **Collection:** `scanning/inventory.js` runs in the main frame and up to 10 subframes, with at most 1,000 items of each kind per frame. Inputs outside forms go into one pseudo-form, and attributes are read with `getAttribute`.
  - **Hints:** input kinds and button intents are assigned in Python from `scanning/form_patterns.yaml`, ignoring case, accents and separators, and matching from word starts. The word lists cover English, Turkish, Polish, Russian and German.
  - **Links:** tags include `a`, `button`, `form`, `iframe`, `meta-refresh`, `redirect` and `navigation`. Links are deduplicated per (url, tag), with at most 200.
  - **Oversize `meta.json`:** links, buttons, forms and text are emptied in that order until it fits in 2 MB.
  - **CTA targets:** `persist_capture` writes the `cta_targets` signal with the pre-check's helpers and promotes candidates through K71's path (at most 10).
- **Why:** The signals read the same YAML, and a JavaScript copy of the patterns would drift. Inputs named `action` or `method` shadow the form's own properties. Promotion (K57, K71) must not be lost at stage 2.
- **Alternatives:** A `scans.links` column with promotion left to the signals; patterns embedded in JavaScript.

### K98 · The hand-off folder

- **Decision:**
  - **Mount:** both compose files mount the hand-off volume at `/artifacts-tmp` (uid 10001, mode 0770).
  - **Writing:** the browser writes to `<job_id>.partial/` and writes `meta.json` last. It deletes any unlisted file, including oversized ones (they are never truncated), and renames the folder to `<job_id>/`.
  - **Hand-over:** `persist_capture` is queued on success, on the last failed attempt, and on redelivery when `<job_id>/` already exists.
  - **Cleanup:** a daily job at 05:00 UTC removes `<uuid>` and `<uuid>.partial` entries older than 24 hours by `lstat` mtime, without following symlinks.
- **Why:** The rename makes the hand-over atomic, so worker-default never sees a half-written folder. Dramatiq can redeliver after a crash. Only worker-default mounts the volume, not the scheduler, and there is a disk alarm (§23).
- **Alternatives:** A path from an environment variable; a bind mount; leaving cleanup to the browser.

### K99 · Worker processes and packaging for the browser stage

- **Decision:**
  - **Workers:** `workers/specs.py` adds `browser` (service `worker-browser`, 2 processes × 1 thread, queue `capture`). `default` consumes `default`, `discovery`, `enrich`, `precheck`, `persist` and `signals`, among others.
  - **Imports:** `kalkan.workers.capture` imports Playwright only while capturing. The browser sends `persist_capture` by name and never imports worker-default code.
  - **Broker:** the browser's broker has no Results middleware.
  - **Settings:** worker-browser requires only `REDIS_URL`. `PROXY_MAP` is optional (K94).
  - **Packaging:** `playwright` is in the `browser` dependency group. The backend image installs no default groups; the browser image adds `browser`. `/ms-playwright` is not writable by the group or others.
- **Why:** Each queue belongs to one worker (K30), and Playwright's sync API is thread-bound. worker-default and the scheduler must be able to send jobs without Playwright installed. The browser's Redis user cannot reach result keys (K102). Without `PROXY_MAP`, jobs wait instead of the worker crash-looping. A compromised browser must not replace binaries that later jobs run.
- **Alternatives:** Running the browser in worker-default; Playwright as a main dependency; a `compute_signals` queue without a consumer.

### K100 · Reference screenshots of the official site

- **Decision:**
  - **Triggers:** the outbox job `refresh_brand_references` is written when a primary official domain is added (demo excluded). It also runs from `kalkan brand references` and weekly on Mondays at 04:00 UTC.
  - **Job:** it captures the brand's oldest primary domain. Only `ok` viewport screenshots are stored, as `reference_desktop` and `reference_mobile` in `brand_assets`, under `brands/{brand_id}/refs/{issued_at}/{profile}.png`.
  - **History:** old references stay. Errors and challenges produce no reference and are only logged.
- **Why:** One current look is enough to compare against, even for brands with many domains. In practice, "when the brand is added" means "when its primary domain is added". A challenge or error screenshot would poison the pHash.
- **Alternatives:** A job per official domain; capturing on every domain addition.

### K101 · Reconciler rule for evidence

- **Decision:** The reconciler issues a new capture job (`issue_case_capture`) for a case when all of these hold:
  - it is `detected` and not a demo case;
  - its last `prechecked` event since entering `detected` is `route=capture` and at least 2 hours old;
  - there is no `ok` browser scan since then;
  - it has no open review task and no `job_failed` since entering (`enrich_domain` excepted).

  The issuer skips cases with a pending marker. `Reconciled.captures` counts only jobs actually issued.
- **Why:** The pre-check does not rescan a case it sent to stage 2 (K74). Lost, dropped or expired jobs are rebuilt from case state (§2), and deferred jobs are protected by the marker.
- **Alternatives:** Trusting the pre-check's job alone; re-sending without a marker.

### K102 · The browser's Redis ACL

- **Decision:** The `browser` Redis user may run only the commands Dramatiq 2.2's Redis broker needs for consuming, acking, nacking, requeueing, delayed messages, heartbeats and maintenance (`EVALSHA`, `SCRIPT LOAD`, `CLIENT SETINFO` and a short list of list, hash and sorted-set commands). It may touch only these keys:
  - its own queue (`capture`) and the queue it sends to (`persist`);
  - the heartbeat and ack keys;
  - the `kalkan:proxy:*` byte counter, with only `GET`, `INCRBY` and `EXPIRE`.

  There is no MULTI/EXEC, PING, SELECT, pub/sub, `capture:*` or `dramatiq-results:*`. `backend/tests/integration/test_redis_acl.py` starts its own Redis with this file. A real worker defers, retries, hands over, dead-letters and counts, and the ACL log stays empty, while forbidden commands get NOPERM.
- **Why:** A compromised Chromium may perform queue operations only. It must not read job records, delete markers or inject jobs. The counter is shared by both browser processes. K82's first rule broke Dramatiq on the `dramatiq` key itself.
- **Alternatives:** Moving the counter to worker-default with read-only access; broader category-based rules.

### K103 · Browser metrics

- **Decision:**
  - **Metrics:** worker-browser exports `captures_total{outcome}`, `proxy_bytes_total` and `capture_deferrals_total{reason}` (`no_proxy`, `proxy_daily_limit`, `browser_unavailable`) through Dramatiq's Prometheus exporter on 9191.
  - **Scraping:** Prometheus scrapes it with tight limits (sample count, body size, label count and length). An oversized scrape becomes `up=0` instead of growing Prometheus's memory.
  - **Firewall (reference VM):** a single rule lets Prometheus reach the browser on 9191, connections in the other direction are rejected, and `br_netfilter` is enabled.
- **Why:** The "proxy configuration missing" alarm needs a metric, and only the browser can produce it. Docker 29 sends same-bridge traffic through iptables only in some network modes.
- **Alternatives:** Metrics from worker-default; sending deferrals as messages.

### K104 · Chromium's sandbox under Docker

- **Decision:** `infra/compose/vm/seccomp-chromium.json` is Docker's default seccomp profile plus unconditional `chroot`, `clone`, `setns` and `unshare`; `clone3` still returns ENOSYS, and glibc falls back to `clone`. worker-browser runs with `cap_drop: ALL`, `no-new-privileges` and this profile in every compose file. Renderers run in new user and PID namespaces under Chromium's own seccomp filter. `--no-sandbox` appears nowhere. On hosts whose AppArmor restricts unprivileged user namespaces (Ubuntu 23.10+, GitHub runners), CI loads an AppArmor profile for the headless shell, and self-hosted servers allow user namespaces (`deploy/selfhost/README.md`). The restriction is never worked around by turning the sandbox off.
- **Why:** With Docker's default profile Chromium reports "No usable sandbox!". Playwright's documented profile fails at `chroot` under `cap_drop: ALL`. The sandbox stays on (§10, §24).
- **Alternatives:** `--no-sandbox`; `CAP_SYS_ADMIN`; Playwright's seccomp profile; turning the restriction off on the runner.

### K105 · Local browser path through dev-proxy

- **Decision:**
  - **Proxy:** `dev-proxy` is tinyproxy pinned by digest, for development and tests only. It has a read-only root filesystem and no capabilities.
  - **Filtering:** it serves only `\.test$` hosts (`FilterDefaultDeny Yes`, everything else gets 403) and connects only to port 443 for CONNECT. It logs every request line, and there is no stats host and no basic auth.
  - **Network:** the local worker-browser is in the `browser` profile (`make up-browser`), on an `internal: true` network with Redis and dev-proxy only. `.env.example` sets `PROXY_MAP='{"PL":"http://dev-proxy:8888"}'`.
  - **Fixtures:** `mintera-loop.test` (redirect and reload loops) and `mintera-heavy.test` (about 30,000 px, 60 images, 250 links, a video).
- **Why:** The proxy path works locally too. There is no firewall locally, and an unfiltered proxy would reach Postgres, Redis and fake-gcs. The internal network makes fail-closed physical. The browser image is large, so `make up` stays light without it.
- **Alternatives:** Our own Alpine image; basic auth on dev-proxy; running the tests inside the browser image.

### K106 · `CZDS_TLDS=*` means every approved zone

- **Decision:** `CZDS_TLDS=*` scans every approved TLD, com and net included (`discovery/czds.select_links`). An empty value means every approved zone except com and net. The reference VM uses `*`.
- **Why:** In production the two largest zones should be scanned too, and every newly approved TLD should enter without a list change.
- **Alternatives:** A TLD list in production; changing the meaning of the empty value.

### K107 · Compose security test

- **Decision:** `backend/tests/unit/test_compose_security.py` checks the compose files without Docker:
  - worker-browser's environment has no `DATABASE_URL*`, `GOOGLE_*`, `CLOUDSDK_*`, `GCS_*`, `GCP_*`, `CLOUDSQL_*` or `STORAGE_EMULATOR_HOST`;
  - it gets no `app.env`, mounts only the hand-off volume, and has no ports, privileges or added capabilities;
  - its sandbox settings and command are as expected;
  - it is on the expected networks only;
  - its secrets are a subset of `REDIS_URL`, `PROXY_MAP` and `SENTRY_DSN`;
  - it is not on the metadata allow list;
  - the seccomp profile equals Docker's plus the namespace calls.
- **Why:** The absence of credentials is proven on the configuration and cannot regress silently.
- **Alternatives:** Inspecting running containers with Docker.

### K108 · More defenses against an untrusted browser

- **Decision:**
  1. A `QueueGuard` middleware in every worker (`workers/broker.py`) runs a message only if its actor belongs to the queue it came from. On queues the browser can write (`persist`), it strips `pipe_target`, `pipe_ignore`, `on_success` and `on_failure`.
  2. Chromium starts with an allow-listed environment (K95).
  3. Prometheus scrapes the browser with limits (K103).
  4. A file kind in `meta.json` that the browser never writes counts as a validation error, without retry.
- **Why:** Dramatiq finds an actor by name alone. A compromised browser that can write to `persist` could otherwise run any worker-default actor, or chain jobs with pipelines and callbacks. Renderers inherited the worker's environment, and a memory scan of a renderer found the proxy password. A browser exporting millions of series could kill Prometheus on every scrape.
- **Alternatives:** A Redis instance per queue; signed actor names; reading secrets from files without cleaning the environment.

### K109 · One Chrome identity for both stages

- **Decision:** The HTTP pre-check takes its User-Agent and market languages from the browser profiles (`profiles.user_agent(Profile.DESKTOP)`, `profiles.MARKETS`).
- **Why:** A fixed older Chrome version in stage 1 differed from the browser's. A kit that cloaks by version could show stage 1 a harmless page and push the case to watching.
- **Alternatives:** Two separate tables.

### K110 · Reference page text

- **Decision:** `brand_assets.text_excerpt` holds the visible text (up to 20,000 characters) of an `ok` reference profile. `text_similarity` compares the candidate's text with the newest reference text of the same profile, or else the other profile.
- **Why:** The official page's text was not stored anywhere, and a reference job has no case or scan.
- **Alternatives:** A separate stored object; refetching the official site for every case.

### K111 · Buttons stored with scans; signals reclassify raw attributes

- **Decision:**
  - **Storage:** `scans.buttons` (jsonb) stores the browser's buttons outside forms.
  - **Reclassification:** signals ignore the browser's `kind` and `intent` hints. Every field is reclassified from its raw attributes (type, name, id, label, placeholder, autocomplete), and every button from its text, with `form_patterns.yaml`.
  - **Signal definitions:**
    - `form_card`: a card number or security code field; an expiry date alone is not enough.
    - `deposit_cta`: a button inside or outside a form whose intent is `deposit` (paying in or topping up).
    - `external_form_action`: the form's action host has a different registered domain from the page's final URL. Hidden fields count too, and visible ones are listed separately in the detail.
- **Why:** Payment buttons are often outside forms. The browser is untrusted (K82), and a compromised one could flip a signal through its hints.
- **Alternatives:** Computing signals from `meta.json` at persist time; trusting the hints.

### K112 · Screenshot hashing

- **Decision:**
  - **Preparation:** grayscale, crop the top to a square (height at most the width), `GaussianBlur(max(1, width // 200))`, `ImageOps.equalize`, then 64-bit `imagehash.phash` and `dhash`. References get the same preparation, while favicons and logos are hashed as they are. Transparency is flattened onto white.
  - **Limits:** images over 40 megapixels, or ones Pillow cannot read, are not hashed (decompression bombs).
  - **Tests:** the fixture screenshots in `fixtures/screenshots/` (made by `tests/tools/screenshot_fixtures.py`) pin the result. The clone is at least 0.85 similar to the official site, and the unrelated and parked pages stay below 0.60.
- **Why:** Plain pHash put a clone and an unrelated page at similar distances. On dark, flat backgrounds most DCT coefficients sit near the median, so a small layout shift flips bits. Squeezing a long mobile page into a square lost the brand's top section.
- **Alternatives:** Plain pHash; larger hashes (which change the 1 − d/64 definition); hashes per tile; whash or average hash.

### K113 · How `compute_signals` writes

- **Decision:**
  - **Placement:** scan-level signals go to the profile's newest `ok` browser scan. Combined and external signals go to the case (`scan_id` empty, K40).
  - **Missing inputs:** a signal that cannot be computed is written with an empty value and `detail.reason` (`no_reference`, `no_screenshot`, `no_capture`, `no_favicon`, `no_text`, `one_profile`).
  - **Idempotence:** values and details contain no times, and a second run updates rows in place.
  - **Definitions:**
    - `favicon_phash_distance` uses the pHash the pre-check stored for the candidate favicon.
    - `cloaking_suspected`: both profiles are `ok`, the Jaccard overlap of their title and text words is below 0.5, and the brand appears in only one of them.
  - **Hashing:** the actor first hashes any unhashed brand assets inline.
- **Why:** The snapshot then shows every signal name, and a missing reference is not mistaken for "not similar". Idempotence is an acceptance criterion. The favicon file is not stored as evidence, so its hash is taken during the pre-check.
- **Alternatives:** Not writing uncomputable signals; storing favicons as artifacts; downloading the favicon again.

### K114 · The AI queue before worker-ai existed

- **Decision:** Before worker-ai existed, `ai_triage(case_id)` was sent by name to the `ai` queue, where messages waited for a consumer.
- **Why:** This followed the skeleton rule for not-yet-built stages: messages were kept, not lost.
- **Alternatives:** A logging skeleton in worker-default, which would have lost the messages.
- **Status:** worker-ai consumes the queue (K128). Without AI, messages go to stand-ins instead (K184).

### K115 · Reconciler rule for signals

- **Decision:** The reconciler sends `compute_signals` for a case when all of these hold:
  - it is `detected` and not a demo case;
  - it has no open review task and no `job_failed` since entering;
  - a profile's newest `ok` browser scan started after entering `detected` and at least 30 minutes ago;
  - that scan has no `form_password` signal.
- **Why:** `compute_signals` writes `form_password` for every scan it reads. Older scans never get it, so only the newest is checked; otherwise the case would be re-sent every 10 minutes.
- **Alternatives:** Looking at the time of the case-level `visual_similarity` (the upsert keeps `created_at`).

### K116 · `imagehash` as a main dependency

- **Decision:** `imagehash` is a main dependency. It brings Pillow, NumPy, SciPy and PyWavelets, which makes the backend image larger.
- **Why:** pHash's DCT comes from SciPy, and the library is well known.
- **Alternatives:** Hand-written pHash and dHash on Pillow and NumPy.

### K117 · Staff sign-in

- **Decision:**
  - **Sessions:** the password step opens a 10-minute pending session (`mfa_passed=false`). The TOTP step deletes it and opens a full session with a new token (7 days, ended by 12 hours of inactivity). `last_seen_at` is written at most once a minute.
  - **Lockout:** password and TOTP failures go to `auth_attempts`. Five failures in 15 minutes for one e-mail or IP lock sign-in for 15 minutes, doubling with every five more that day, up to 24 hours.
  - **CSRF:** double-submit cookie at sign-in. After sign-in the token is `sha256("kalkan-ops-csrf:" + session token)`, sent as a form field or the `X-CSRF-Token` header.
  - **Headers:** pages use CSP `default-src 'self'` (no inline scripts or styles), `frame-ancestors 'none'` and `no-store`. HTMX is vendored.
- **Why:** §24 requires TOTP, attempt limits and CSRF. Upgrading a pending token in place would allow session fixation. The panel is reached only through a tunnel, so SameSite=Strict is no burden.
- **Alternatives:** Sharing the customer API's code; sessions in Redis; a fixed lock time.

### K118 · Serving files in the ops panel

- **Decision:**
  - **Serving:** files are streamed through the panel. PNG, JPEG, GIF, WebP and ICO are shown inline. Everything else from candidates, SVG included, is an `application/octet-stream` attachment. Operator-uploaded SVG logos are shown inline.
  - **Headers:** every file response carries `Content-Security-Policy: sandbox; default-src 'none'`.
  - **Uploads:** the type is detected from the content. Limits: logos and favicons 5 MB, LOAs (PDF, PNG, JPEG) 15 MB, whitelist CSVs 2 MB. File names are reduced to `[A-Za-z0-9._-]`.
- **Why:** Scanned HTML and SVG could run scripts on the panel's origin, and the type a browser reports is untrusted.
- **Alternatives:** Signed URLs; showing HTML in a sandboxed iframe.

### K119 · Failed jobs in the ops panel

- **Decision:**
  - **List:** the panel shows the last 24 hours of `job_failed` events.
  - **Re-queueing:** `http_precheck`, `compute_signals` and `enrich_domain` are re-queued through the outbox. A capture failure of a `detected` case gets a new capture job. A case already in review gets new evidence from the review screen.
  - **Records:** every re-queue writes a `note` event (`action=requeue`) and `audit_log`. The reconciler ignores a `job_failed` that is followed by such a note.
- **Why:** Per §7, these cases wait for an operator. Without the note, the reconciler would skip the case forever.
- **Alternatives:** Deleting the failure event; a new event type.

### K120 · Clearing blocked cases

- **Decision:**
  - **Job:** `clear_blocked_cases(customer_id?)` runs on the `notify` queue.
  - **Triggers:** a customer's monitor-only or test flag is turned off, its sending or the global switch is turned on, its dry-run is turned off, or an LOA is recorded.
  - **Effect:** `approved` cases lose their `blocked_reason`, and the sender checks every gate again.
  - **Safeguards:** flag and LOA changes need a server-checked confirmation box. A test customer cannot leave dry-run, and a database constraint enforces it.
- **Why:** This is §7's clean-up job. Clearing only the reason is harmless.
- **Alternatives:** Leaving it to the sender; trusting the browser's `confirm()`.

### K121 · Labelling queue

- **Decision:**
  - **Queue:** non-demo cases with at least one screenshot and no label, in case-number order. N skips, and skipped cases come back at the end.
  - **Labels:** one label per case. Relabelling changes the category and note, and `audit_log` records the old and new values.
  - **Shortcuts** (`static/ops.js`): 1 to 6 save and move on; N skips; `/` opens the note; Esc leaves the field. Digits do not label while the note field has focus.
  - **Target:** 200 labeled cases.
- **Why:** The evaluation set (product.md §10) needs a consistent order and no duplicate views.
- **Alternatives:** Random order; a label history table.

### K122 · Settings store

- **Decision:** `db/settings_store.py` defines the types and validation of every `settings` key in one place: switches, ratios from 0 to 1, and channel limits from 0 to 10,000. The panel writes only known keys, records `updated_by` and `audit_log`, and switches need a confirmation box.
- **Why:** The decision engine and the sender read the same definitions.
- **Alternatives:** A free-form JSON editor.

### K123 · Structured output with code-side validation

- **Decision:**
  - **Request:** `messages.create` with `output_config.format` set to a JSON schema built by `anthropic.transform_schema(Verdict)`.
  - **Validation:** the answer is checked in code with `Verdict.model_validate_json`. Enums are compared in lower case, `confidence` must be 0 to 1, and `evidence` has at most 12 items.
  - **Retry:** a refusal, `max_tokens`, a missing text block or a schema mismatch is asked once more. A second failure raises `InvalidOutputError`, and the case goes to review as `ai_unavailable`.
  - **Usage:** the tokens of invalid answers are still recorded in `ai_calls`.
- **Why:** `messages.parse` validates inside the response parser, and on failure the response and its `usage` are lost. Paid tokens must count against the budget.
- **Alternatives:** `messages.parse` and estimating the cost of invalid answers.

### K124 · Retry loop in our code

- **Decision:**
  - **Retries:** the SDK's retries are off (`max_retries=0`). The code makes 3 attempts of up to 60 seconds, with back-off of 2 and 4 seconds plus jitter and `retry-after` honored up to 30 seconds. Connection errors, 408, 409, 429 and 5xx (529 included) are retried. Other 4xx errors become `ai_unavailable` at once.
  - **Records and limits:** every attempt is one `ai_calls` row. A process makes at most 4 concurrent requests, and AI actors have a 300-second time limit.
  - **Parameters:** no `temperature`, `top_p` or `top_k`. `effort` is `low` for triage and `medium` for confirmation, and `max_tokens` is 4096.
- **Why:** §13 requires every call in `ai_calls`, failures included, and the SDK's internal retries are invisible. Three 60-second attempts do not fit in 120 seconds. Since October 2026, sampling parameters return 400.
- **Alternatives:** SDK retries; shorter timeouts.

### K125 · Monthly AI budget

- **Decision:** Before every call (and before uploading brand images), this UTC month's `ai_calls.cost_usd` is compared with `AI_MONTHLY_BUDGET_USD`, evaluation calls included. When it is reached, no request is made: the case goes to review (`ai_budget_exhausted`), `ai_budget_stops_total` increases, and a warning is logged. A model without a known price is never called.
- **Why:** §13 sends cases to review rather than waiting, and evaluations spend real money too.
- **Alternatives:** Deferring the job; a separate evaluation budget.

### K126 · Prompt layout and caching

- **Decision:**
  - **Brand block:** the system prompt is followed by a brand block. It holds the brand's name, keywords, markets, languages and official domains, its whitelist entries (at most 50), and each labeled image (the newest light and dark logos and the desktop and mobile references). One `cache_control` (`ttl: 1h`) on its last block caches the system prompt and the brand block together.
  - **Candidate block:** then the candidate images ("Image 1: candidate desktop", "Image 2: candidate mobile"), then the candidate text:
    - domain, match and the code's signals (yes/no and numbers only; nothing with URLs);
    - the reclassified form inventory;
    - `<untrusted_domain_data>` (RDAP, DNS, hosting);
    - `<untrusted_page_content>` (title, final URL, redirects, button texts, field labels and page text, at most 4,000 characters for all pages).
  - **Untrusted text:** `<` and `>` become `‹` and `›`, and control characters are removed.
  - **Version:** `prompt_version` is the first 12 hex digits of SHA-256(`LAYOUT` + the three prompt files).
- **Why:** The system prompt alone is below the minimum cacheable size. Brand cases arrive sparsely, so a 5-minute cache would mostly miss. RDAP data is untrusted too, and content without angle brackets cannot close its own tag.
- **Alternatives:** A 5-minute cache; caching the system prompt separately; escaping only the closing tag.

### K127 · Images through the Files API

- **Decision:**
  - **Brand images:** uploaded with the Files API on first use, and the `file_id` is stored in `brand_assets.anthropic_file_id`. Asset rows never change, so a new logo is a new row and uploads itself.
  - **Formats:** JPEG, PNG, GIF and WebP go as they are when within 1568 px and 5 MB. Other formats, ICO included, are scaled and converted to PNG, or to JPEG above 5 MB. SVG and unreadable images are skipped.
  - **Failures:** a failed upload sends the image inline for that call, and the next call retries the upload.
  - **Candidate screenshots:** always inline (base64).
- **Why:** The Files API accepts only JPEG, PNG, GIF and WebP, and smaller images cost fewer tokens.
- **Alternatives:** Base64 brand images on every call; uploading candidate images too.

### K128 · worker-ai

- **Decision:**
  - **Process:** one process with 4 threads on the `ai` queue. Its actors are `ai_triage(case_id)` and `ai_confirm(case_id)` (`workers/assess.py`).
  - **Flow:** a stored verdict sends `decide` by name to the `decision` queue.
  - **Idempotence:** triage runs only if the case's newest `form_password` signal is newer than its last triage verdict. Confirmation runs only if the newest decision is `confirm` with no confirmation verdict after it. A redelivered message does nothing.
  - **Scope:** a verdict never changes the case's state or category; the decision engine does that.
  - **Reconciler:** it re-sends triage or confirmation that is more than an hour overdue.
- **Why:** The queue belongs to one process (K30). Enrichment's case signal is written before capture and must not trigger triage.
- **Alternatives:** Writing the category from the verdict at once; triaging on every `compute_signals`.

### K129 · Evaluation harness

- **Decision:**
  - **Export:** `kalkan eval export` writes labeled, non-demo cases as JSONL (meta line, then per case the label, snapshot, whitelist summary and image paths), with the images next to it.
  - **Run:** `kalkan eval run` builds the production prompt (images inline, effort `low`, 4 parallel requests). With `--batch` it uses the Message Batches API.
  - **Accounting:** with a database, calls are recorded in `ai_calls` (`purpose=eval`) and count against the budget.
  - **Report:** a confusion matrix with no-answers as a separate column, precision and recall per category, accuracy on the cases rule R1 would send to confirmation (using `decision/rules.py`), cost, tokens and wrong answers. Several models give a comparison report.
  - **Git:** datasets and reports stay out of git.
- **Why:** Screenshots of real sites must not enter a public repository. A dataset must work with another API key, because Files API ids are bound to a workspace. R1 has one source, the decision package.
- **Alternatives:** Datasets with Files API ids; copying R1 into the harness.

### K130 · Prices as configuration

- **Decision:**
  - **Prices:** `AI_PRICES` (JSON) replaces the whole price table, whose default is in `ai/prices.py`. Haiku 5.5's tier above 100K tokens looks at the whole prompt and applies to the whole call. The batch discount applies to cache prices too.
  - **Local use:** worker-ai runs in the `ai` profile (`make up-ai`) and refuses to start without a key. `make eval DATASET= MODELS= LIMIT=5` runs evaluations.
- **Why:** Prices change, and it must be possible to fix them without a deploy.
- **Alternatives:** Prices in code.

### K131 · Recapture

- **Decision:** The review screen's C key moves a case from `in_review` back to `detected` (event reason `recapture`, and the open task closes with a note), then issues a new capture job. Evidence, signals, triage and the decision run again.
- **Why:** When an operator wants new screenshots, the case must pass the rules again. Every stage after capture looks only at `detected` cases.
- **Alternatives:** Capturing new screenshots while the case stays in review.

### K132 · Operator decisions

- **Decision:**
  - **Approve:** the operator picks the basis (P phishing, T trademark), and the case becomes `verified` and `approved` (`approved_by` = the operator's e-mail, `auto_approved` false).
  - **Approve and Reject** write a `staff` verdict. Its `suggested_basis` is the notices' basis (`none` on Reject). The category comes from the basis on Approve (phishing → `phishing_clone`, trademark → `brand_impersonation`) and from the operator's choice on Reject.
  - **Verify only** leaves the case `verified` (decision outcome `verify`). **Watch** sets the next check to +7 days, and **Reject** to +30 days.
  - **Labels:** "Save as a label too" is on by default.
  - **Records:** every action writes `decisions`, events and `audit_log`.
- **Why:** No new column is needed for the basis, because the sender reads the newest verdict's basis. "Verify only" is neither an approval nor a review.
- **Alternatives:** A `cases.basis` column; recording Verify as a review outcome.

### K133 · `decide`

- **Decision:**
  - **Queue and guards:** `decide` runs in worker-default on the `decision` queue. It exits unless the case is `detected` and a new verdict exists since its last decision. Verdicts and decisions are counted since the last entry into `detected`.
  - **After triage:**
    1. R2 (review) when there is no verdict, no challenge or `ok` scan, a conflict, or `official_infrastructure` is not false;
    2. R1 → `confirm`;
    3. R5 (+1 day);
    4. R3 (+7 days);
    5. R4 (+30 days);
    6. R2.
  - **After confirmation:** C3 (review) on a conflict or when the models disagree. Otherwise C2 when auto-approval is off, or C1 → `verified` and `approved` (`approved_by=rule`) when it is on.
  - **Conflicts:**
    - the model sees a form the code did not find;
    - the model says "not a threat" while the code found a form and visual or favicon similarity to the official site;
    - the model says "parked" while the code found a form.
  - **Reconciler:** a verdict without a decision for 30 minutes gets `decide` again.
- **Why:** K64 requires null to be treated like true, and R1 requires false. Conflicts come first so that R3 or R4 never hide a form.
- **Alternatives:** Looking for conflicts only in R2; treating unknown official infrastructure as false.

### K134 · Review queue keyboard

- **Decision:**
  - **Queue:** non-demo cases in `in_review` with an open review task, in case-number order. N skips, and skipped cases come back at the end.
  - **Shortcuts** (`data-keys`): single keys (V, W, C) and two-key chords (A+P/T, R+1…6). The pending key is shown on screen and Esc cancels, so every decision takes at most 3 keys.
  - **No-script use:** every button is a plain form post, so the page works without scripts.
- **Why:** These are §19's shortcuts. Choosing a basis is mandatory on approval (K56).
- **Alternatives:** A separate basis screen; HTMX partial updates.

### K135 · Notice planning hook

- **Decision:** Approval calls `notices/planner.py plan_notices(case_id)`, both from the rule path and from the review page.
- **Why:** Planning had a single entry point from the start.
- **Alternatives:** An outbox job from the start.
- **Status:** Planning moved into the approval transaction (K137).

### K136 · Registry channel

- **Decision:** `registry` is a notice channel and a `channel_contacts` kind, with a daily limit of 20. A registry contact is keyed by the lower-case A-label TLD (`online`, `xn--p1ai`) and comes only from `channel_contacts`. Without one, the registry step becomes an operator task. The gates read: no LOA → `no_loa`; legal text not approved → `legal_review`.
- **Why:** Neither IANA's nor the registries' RDAP records carry the registry's abuse role. IANA's contacts are administrative, and a registry's RDAP abuse contact is the registrar's. The key must not go through ASN or IANA id normalization, because `as` and `asia` are TLDs.
- **Alternatives:** Guessing a registry address from RDAP; a `domains.registry_abuse_email` column.

### K137 · Notice flow

- **Decision:**
  - **Approval:** the approval (`decide` and the review page) leaves a `plan_notices` outbox job in the same transaction.
  - **Planning:** `plan_notices` only writes to the database. First-wave rows are `draft`, with round `reopened_count + 1` and the effective `dry_run`, and a repeat writes nothing new. It passes new, blocked (except sent previews) and stuck rows to `ai_notice_text`. Monitor-only customers get no rows.
  - **Text:** `ai_notice_text` checks gates 1 to 3 first and calls no AI for a blocked notice. It writes the explanation, queues the row and sends `send_notice`.
  - **Sending:** `send_notice` locks the case and the row, renders the text once with a fresh evidence link, and applies the gates. A preview row stays `blocked/dry_run` and keeps its preview Message-ID.
  - **Unblocking:** `clear_blocked_cases` skips demo customers and plans again for every case it clears.
  - **Templates:** `templates/{template}.{lang}.j2`, with channel differences inside. Subjects and short strings are in `templates/strings.json`.
- **Why:** No work is lost between approval and planning. Planning must make no network calls inside the ops process or `decide`'s 30 seconds. Blocked rows are retried when a gate lifts, and 12 template files are easier to maintain than 48.
- **Alternatives:** Planning in a separate call after approval; one template file per channel.

### K138 · The explanation paragraph

- **Decision:**
  - **Generation:** `AI_TEXT_MODEL` with effort `low` writes the paragraph in English and the target language in one call, with its own prompt files and `prompt_version`. Its input is only the fields listed in §13; registrar and hosting names go inside `<untrusted_registration_data>`.
  - **Validation:**
    - at most 900 characters;
    - none of `<>`, `` ` ``, `*`, `#`, `[]`, `{}`, `|`, `\`, `@`, `http`, `www.`, `://`, `xn--`, `[.]`, `(dot)` or ` dot `;
    - every host-like word must be the case domain, the final URL's host or the evidence link's host.
  - **Fallback:** if validation fails, the AI errors or the budget is spent, a fixed paragraph is used (`templates/_explanation.{lang}.j2`, our fields only). `notices.explanation_source` records `ai` or `fallback`.
- **Why:** §13 and §24 call for a defense against second-order prompt injection: no free text goes into the model or comes out of it. Notices must not stop when the AI does.
- **Alternatives:** One call per language; keeping the explanation only in the body.

### K139 · E-mail providers

- **Decision:**
  - **Interface:** `EmailProvider` has two implementations. `PostmarkProvider` uses the Email API on the transactional `outbound` stream, `TextBody`, no tracking, the Message-ID in `Headers`, and Postmark's id in `external_ref`. `SmtpProvider` uses the standard library.
  - **Message-ID:** made with the sender's domain before sending and stored in `provider_message_id`.
  - **Errors:** Postmark 422 with ErrorCode 300 or 406 is permanent (`failed`). 401, 412, 413, 429, 5xx and connection errors are temporary: the job retries and the row stays `queued`.
  - **Bodies:** notices are plain text. Invitations, password resets and customer alerts use `email/templates/*.txt.j2`, whose first line is `Subject:`.
- **Why:** Account problems, such as a provider account awaiting approval, must not mark notices permanently `failed`, because the follow-up stage reserves `failed` for bounces. A developer machine must never reach a real recipient.
- **Alternatives:** SES; HTML bodies.
- **Status:** The original rule "local means SMTP only, production means Postmark only" was replaced by K181.

### K140 · Evidence links

- **Decision:**
  - **Tokens:** a separate link per notice, made when its text is first rendered and valid for 90 days. The token appears only in the body, and its hash is stored in `evidence_links`.
  - **Endpoints:** `/e/{token}` and `/e/{token}/files/{artifact_id}` resolve the token, then run as `kalkan_api` under RLS and serve only that case's files. PNG, JPEG, GIF and WebP are shown inline, and everything else is a download.
  - **Headers:** every response carries a sandbox CSP, `X-Robots-Tag: noindex`, `no-store` and `no-referrer`.
  - **Content:** the page shows the case number, domain, first seen, screenshots, every file with its SHA-256, the registrar, the registration date, name servers, IPs and hosting.
  - **Rate limit:** 60 requests per minute per IP, in process memory. The IP is the first hop of `X-Forwarded-For`, which the edge (Worker or Caddy) sets.
  - **Expiry:** expired links do not resolve.
- **Why:** The API has no Redis (K3). A link per notice can be revoked without affecting other notices.
- **Alternatives:** One link per case; a database-backed rate limit.

### K141 · Retractions and customer alerts

- **Decision:**
  - **Retraction:** when a whitelist entry dismisses a case, every real notice already sent (sent, acknowledged, manual pending) gets a `retraction` row on the same channel, to the same recipient and in the same language, with no AI. Retractions pass only the dry-run, kill-switch and legal gates. If blocked or web-form only, they become an operator task. Once sent, the original is marked `retracted`.
  - **Customer alert:** goes once (`customer_alert_sent_at`) when a case becomes `verified` as a `phishing_clone`, whether by rule or by an operator, "Verify only" included. There is no alert without `alert_email`. In dry-run it goes to the redirect address with "[DRY RUN]".
- **Why:** A retraction corrects a wrong notice, so it must not wait because the customer's own sending is off or the account is suspended. A phishing clone that an operator only verified still matters to the customer.
- **Alternatives:** Applying every gate to retractions; alerting at approval.

### K142 · Follow-up check results

- **Decision:** `followup/rules.py` classifies each check:
  - **Down:** NXDOMAIN or SERVFAIL, HTTP 404, 410 or 451, a suspension page, or a third connection error in a row.
  - **Warning:** a Cloudflare or browser warning page, or an open site listed in Web Risk (with `WEBRISK_API_KEY`).
  - **Up:** 2xx or 3xx, or the browser hitting a bot wall.
  - **Unknown:** everything else (one or two connection errors, 403, 5xx, non-public addresses). An unknown result neither resets nor completes the 72-hour window, and closing needs the current check to be down too.

  Browser confirmation works both ways. A `notified` site that HTTP does not see as up, and a `taken_down` site that HTTP sees as up, get a recheck capture from the target market. A profile that loads a page wins; if none does, the HTTP result stands. Confirmation scans are stored as new evidence. If the previous confirmation job is still pending, the HTTP result is applied alone.
- **Why:** Geo-blocking and cloaking must not look like takedowns, and a flapping site must not count as down. Follow-up must continue when the browser worker is not running.
- **Alternatives:** Treating unknown as down; confirming only the down direction; waiting for the browser.

### K143 · The follow-up clock

- **Decision:**
  - **Where:** the clock is set in `domain/case_state.transition`.
  - **Intervals:** `notified` +6 hours. `taken_down` +1 day, with `monitor_until` +30 days. `watching` +7 days. `dismissed` +30 days, or never when dismissed by the whitelist. Other states have no clock. Callers may set an earlier time.
  - **Scheduling:** every 15 minutes `schedule_rechecks` picks due non-demo cases with `SKIP LOCKED`, leases them for an hour and sends `recheck`. The check writes the next time.
  - **Light checks:** `watching` and `dismissed` cases get a light HTTP check. A password or card field, or for watched cases the brand on a page that is neither parked nor empty, moves the case back to `detected` and queues a pre-check.
- **Why:** No module has to remember its own clock, and the scheduler does not resend the same case every 15 minutes.
- **Alternatives:** Clocks in each module; a separate schedule table.

### K144 · Reopening

- **Decision:** A `taken_down` site that is seen up, and confirmed, returns to `notified`. A `reopened` event is written, `reopened_count` increases, the closing fields are cleared, and `plan_notices` is queued in the same transaction. It writes the first wave of the new round (`reopened_count + 1`) to the same channels, and the evidence link also shows the new confirmation scans. The reconciler catches reopened cases whose new round was not planned within an hour.
- **Why:** When a site comes back, the same channels hear about it again with the new evidence (product.md §11). Old rounds must not break the unique key.
- **Alternatives:** Re-queueing old notices; leaving the new round to an operator.

### K145 · Reply actions

- **Decision:** In `followup/replies.py`, a Haiku model (`AI_REPLY_MODEL`, effort `low`) returns only a class, a confidence and a short summary. The code picks the action, and no action sends anything.

  | Class | Action |
  | --- | --- |
  | `removed` | The notice becomes `acknowledged`, and a recheck runs at once. |
  | `needs_info` | A task. |
  | `not_our_customer` | The notice is `rejected`, and a `reply` task lists the channels not yet tried. The operator decides on the next channel. |
  | `auto_reply` | Nothing. |
  | `other` | A task. |

  Confidence below 0.6 counts as `other`. Without AI, the class is `other` and a task opens. Replies to dry-run previews are classified and recorded but change no notice. An operator can correct the class in the ops panel, which runs its action again.
- **Why:** Sending decisions stay in `decision/`, and AI output alone never sends anything. Dry-run replies are enough to try the flow.
- **Alternatives:** Automatically sending to the next channel; accepting low confidence.

### K146 · Postmark webhooks

- **Decision:**
  - **Endpoints and auth:** `/api/webhooks/postmark/{inbound,bounce}` use HTTP basic auth (`POSTMARK_WEBHOOK_BASIC_AUTH`, compared in constant time). Without the setting every call is 401. Bodies are at most 64 MB.
  - **API work:** the API stores the event (`webhook_events`) and a `process_webhook_event` outbox row in one transaction. Attachment contents and bounce `Content` are not kept; names, types and sizes are.
  - **Raw mail:** the worker stores it as `messages/<event id>.eml`: Postmark's `RawEmail` when sent, else rebuilt from the parsed fields without attachments.
  - **Matching:** the case comes from the plus address on `REPLY_DOMAIN`, else from the `CASE_PREFIX-…` number in the subject. The notice comes from `In-Reply-To` or `References`, else from the sender's address or domain.
  - **Edge cases:** duplicates (same Postmark `MessageID`) are ignored. Events older than 7 days open a task instead of being classified. Unmatched events wait in the ops panel.
  - **Bounces:** found by `external_ref`, the notice is marked `failed` with the bounce type and description, and a `bounce` task opens. A bounced preview only adds a note.
- **Why:** Postmark retries (only 403 stops it) and may deliver an event more than once. The API should do no processing, attachments would bloat the database, and the raw record's source must be known.
- **Alternatives:** Signature checks (Postmark offers none); processing in the API; keeping attachments.
- **Status:** With `EMAIL_PROVIDER=smtp` (K181), notices go out but replies and bounces are not read in.

### K147 · Escalation

- **Decision:** `check_escalations` runs hourly (`followup/escalation.py`).
  - **Clock:** it starts at the last `sent_at` of the real notices in the current round's first wave.
  - **Levels:** each later level of the plan is applied once per round, on time, and recorded as `registrar_escalated` with level, channel, round and hours. A registry notice is written as a draft and passes the gates; without a registry address it becomes a task. Operator levels open an `escalation` task.
  - **Skips:** no escalation while the site is seen down, or after a `removed` reply in the round.
- **Why:** The 48 and 96 hours in §15 count from the end of the first wave, and a rerun must not duplicate notices or tasks.
- **Alternatives:** Counting from the first notice; a separate escalation table.

### K148 · End-to-end test

- **Decision:** `make e2e` (`backend/tests/e2e/`, not part of `make test`) runs the whole pipeline:
  - The clone fixture is injected as a candidate.
  - The pre-check, a real Chromium through dev-proxy, and `persist_capture` run in a Dramatiq worker.
  - Signals, triage with a recorded AI answer, and the decision run in process. Auto-approval is off, so an operator approves.
  - Notices go to Mailpit over SMTP.
  - "The site was taken down" is simulated by a name that stops resolving. Time is advanced through `now`, and the confirmation job counts as pending, so the HTTP result applies.
  - A second scenario checks previews and the real first wave after dry-run ends.
- **Why:** Every stage is wired with real code while the test stays deterministic and takes seconds. dev-proxy resolves `*.test` itself, so the browser would see a stopped site as up.
- **Alternatives:** The full compose stack with real waiting; actually stopping the fixture site.

### K149 · Customer sign-in

- **Decision:**
  - **Sessions:** after the password, a pending session waits up to 30 minutes for the second factor. TOTP or a recovery code turns it into a full session with a new token (7 days, ended by 12 hours of inactivity). `last_seen_at` is written at most once a minute.
  - **TOTP setup:** without TOTP, sign-in returns `{"mfa": "setup"}`. Setup stores the secret encrypted, and verification enables it and returns 10 one-time recovery codes.
  - **Lockout:** failures go to `auth_attempts`. Five in 15 minutes per e-mail or IP lock sign-in for 15 minutes, doubling with every five more in 24 hours, up to 24 hours (429 with `retry_after`).
  - **Passwords and resets:** passwords need at least 12 characters. Reset requests always return 202, with at most 3 links per user per hour, and a reset ends every session.
  - **Cookie:** `kalkan_session` is HttpOnly, SameSite=Lax and `Secure` outside `ENV=local`.
  - **Mail:** the API sends invitation and reset e-mails itself. A failed send is logged and the request still succeeds.
  - **CSRF:** `X-Kalkan-CSRF: 1` is required on every state-changing request under `/api/v1`.
- **Why:** These are the acceptance criteria (no data without TOTP, the session times, CSRF, attempt limits), built on the ops panel's patterns (K117). The counter lives in the database because the API has no Redis.
- **Alternatives:** Writing `last_seen_at` on every request; sending the e-mails through the outbox (the worker must not learn invitation tokens).

### K150 · Endpoint rules of the customer API

- **Decision:**
  - **"This is our site":** writes a whitelist entry tied to the case's brand (kind chosen by the owner, default `other`) and a `whitelist_changed` outbox job. The worker dismisses the case and sends retractions (K141). A second click returns the same entry.
  - **Whitelist:** additions and CSV imports leave `whitelist_changed`, and deletions do not. A CSV is at most 1 MB and 5,000 rows: valid rows are added, existing ones skipped, and the rest reported with line numbers.
  - **CSV export:** cells starting with `=`, `+`, `-`, `@`, a tab or a carriage return get a leading apostrophe.
  - **Users:** never deleted, only disabled (`disabled_at`), which also ends their sessions. An owner cannot disable themselves. E-mail addresses are globally unique.
  - **Files:** `/brand-assets/{id}` serves comparison images. Images are shown inline, and everything else downloads as `application/octet-stream`, with a sandbox CSP and `Cache-Control: private, max-age=300`. `evidence.zip` streams with a `manifest.json` of SHA-256 hashes.
  - **Report data:** `/reports/{id}/data` requires `X-Report-Token`.
  - **Write protection:** write endpoints return 403 `read_only` for the demo and for suspended customers.
- **Why:** Uniform rules for everything the panel needs, `kalkan_api` writes only to the tables it is allowed, and CSV formula injection is defused.
- **Alternatives:** Deleting users; signed URLs; applying the whitelist inside the API.

### K151 · Metrics

- **Decision:** In `domain/metrics.py` and `api/v1/queries.py`:
  - **Cohort:** cases first seen in the period, inclusive, in the customer's time zone. The default is the last 30 days including today, and the maximum is 366 days. Whitelisted cases do not count as detections and are not listed.
  - **Median time to takedown:** from the first notice to `offline_since`. Changes compare with the previous equal period, and the median's change is signed (−22 means 22% faster).
  - **By channel:** the last real notice before the takedown (no previews or retractions).
  - **Rounding:** to the nearest integer, halves up.
  - **Panel status:** `panel_status` is the single mapping. An `approved` case with a dry-run preview shows as Active, and previews never appear in any response.
  - **Index:** the cohort query uses the `cases (customer_id, first_seen_at)` index, checked by an `EXPLAIN` test.
- **Why:** The definitions in product.md §12 live in one pure place, and the design's numbers are verified against the seed.
- **Alternatives:** Computing in SQL; showing previews with a flag.

### K152 · Committed OpenAPI schema

- **Decision:** `make openapi` writes the schema to `backend/openapi.json` with sorted keys. CI regenerates it and fails on any difference.
- **Why:** The web types are generated from the committed schema, so the schema and the code cannot drift.
- **Alternatives:** Pulling the schema from a running API.

### K153 · Web stack

- **Decision:**
  - **Framework and UI:** Next.js 16 (App Router, Turbopack; the middleware file is `proxy.ts`), React 19, Tailwind 4 and shadcn/ui.
  - **Data and forms:** TanStack Query and Table, openapi-fetch and openapi-typescript, Recharts, react-hook-form and zod.
  - **Tests:** Vitest and Playwright.
  - **Pins:** all versions are pinned. TypeScript stays on 5.9 and ESLint on 9 until typescript-eslint and the React ESLint plugin support the next majors.
  - **Assets:** fonts come from the `geist` package, not Google Fonts at build time (superseded by K188). The TOTP QR code is drawn in the client.
- **Why:** A current, consistent stack. The image build does not depend on external networks, and the TOTP secret never goes to a QR service.
- **Alternatives:** The newest TypeScript and ESLint; `next/font/google`; server-side QR codes.

### K154 · One origin for panel and API

- **Decision:**
  - **Origin:** the panel always calls the API on its own origin. Production goes through the edge (Worker or Caddy), and locally `next.config.ts` rewrites `/api/*` to `API_ORIGIN`.
  - **Edge check:** `proxy.ts` compares `X-Edge-Auth` in constant time when `EDGE_SHARED_SECRET` is set.
  - **Redirects:** pages without a session cookie go to `/login?next=<path>`, and `next` must be a same-site path. A 401 sends the browser to `/login`, or to `/login/mfa` on `mfa_required`.
  - **CSRF:** the client adds `X-Kalkan-CSRF: 1` to state-changing requests.
- **Why:** Cookies and CSRF work the same locally and in production. The bare origin serves nothing on its own, and there are no open redirects.
- **Alternatives:** CORS with separate origins; server-side data fetching with the cookie; changing the invitation link in the API.

### K155 · State in the URL

- **Decision:**
  - **Shared parameters:** only `brand`, `range`, `from` and `to` carry over between pages.
  - **Defaults:** Overview defaults to the last 30 days and Threats to all time. Defaults are not written to the URL.
  - **Chart:** its 7/30/90 buttons change the page's range.
  - **Periods:** days are computed in the customer's time zone, and a custom range is at most 366 days.
  - **Threats filters:** `status`, `category`, `q` (debounced 300 ms), `sort` and `page` (25 per page). Unknown values are ignored, and changing a filter returns to page 1.
- **Why:** A filtered view can be shared and survives a reload, and the chart and the KPIs show the same period.
- **Alternatives:** Local storage; a separate chart period.

### K156 · Screen decisions

- **Decision:**
  - **Threats badge:** shows `/overview`'s `live_threats`.
  - **Live strip:** counts from two `/threats?status=…&page_size=1` requests.
  - **Overview:** "Recent threats" has 6 rows, and the whitelist card shows the last 3 entries.
  - **Responsive menu:** the sidebar becomes a sheet on phones.
  - **Dark theme:** uses the same tokens with text contrast of at least 4.5:1, measured with axe.
- **Why:** These follow the design's layout and texts without API changes, and the menu never leads to a 404.
- **Alternatives:** A dedicated badge endpoint; hiding pages from the menu.

### K157 · Web deployment (reference deployment)

- **Decision:**
  - **Service:** Terraform creates the `web` Cloud Run service with a placeholder image (0 to 2 instances, port 3000, deletion protection, the `kalkan-web` account), and its `web_url` is the Worker's `WEB_ORIGIN`. The container belongs to the deploy, as in K79.
  - **Image:** standalone Next.js on a pinned `node:24` slim base, running as uid 10001.
  - **Smoke test:** a deploy checks that `https://app.<domain>/login` returns 200 and the bare run.app `/login` returns 403.
  - **Tests:** Playwright e2e runs locally against a seeded stack (`make web-e2e`), not in CI. CI checks types, lint, Vitest and the build.
- **Why:** The api's proven split applies to web too.
- **Alternatives:** Managing web fully in Terraform; e2e in CI with compose.

### K158 · API additions for the panel

- **Decision:**
  - **Whitelist preview:** `POST /whitelist/import?dry_run=true` checks a CSV with the same rules and writes nothing.
  - **Next steps:** `ThreatDetail.next_steps` gives `next_check_at` for followed cases and, for `notified` ones, the next escalation's time and channel. The plan shape comes from `notices.planner.ESCALATIONS`, which `plan_levels` uses too.
  - **Similarity fallback:** without case-level similarity, the newest scan's value is used.
  - **File names:** a single download uses the same name as inside the ZIP.
- **Why:** The design's "next check" and "Scheduled" rows come from real data, and the preview rules live in the API only.
- **Alternatives:** Validating in the browser; computing escalation times in the panel.

### K159 · Threat detail screen

- **Decision:**
  - **Timeline:** events are translated for customers. `approved` is hidden as an internal step, warnings and reopenings are highlighted, and the last step is "Now".
  - **Notices card:** shows the planned escalation as a "Scheduled" row. For phishing the 48-hour step is the registry (§15, K56), even though the design shows a registrar.
  - **Signals:** at most four customer-readable items, in this order:
    1. logo;
    2. layout (visual ≥ 0.85 or text ≥ 80);
    3. favicon;
    4. brand in the domain (≥ 80);
    5. brand in the title;
    6. card form;
    7. external form;
    8. Web Risk.
- **Why:** This keeps the design's texts and layout without contradicting the business rules.
- **Alternatives:** Copying the design's registrar row; showing raw event types.

### K160 · Roles in the panel and e2e writes

- **Decision:**
  - **`accessOf(me)`:** viewers see no write controls. Owners of read-only workspaces (the demo or a suspended customer) see them disabled, with an explanation for suspended accounts.
  - **e2e writes:** the demo is read-only, so e2e write flows (mark ours, whitelist edits, CSV import) use `page.route` to answer `/me` as a writable owner and to answer the writes (`e2e/support.ts`).
  - **API refusals:** tested against the real API: viewers get 403 `role`, and the demo owner gets 403 `read_only`.
- **Why:** A second writable seed customer would enter the pipeline and scan real domains, so the demo must stay read-only.
- **Alternatives:** A separate writable test customer; a writable demo.

### K161 · Other panel decisions

- **Decision:**
  - **Password reset:** `/forgot-password` (always the same answer) and `/reset-password?token=` are public and never accepted as `next`.
  - **Next.js:** `next.config.ts` sets `agentRules: false`, so `next dev` does not write generated files into `web/`.
  - **Contrast:** in the dark theme the primary color is `#3d5ef5`, and hover uses a darker `--primary-hover` instead of transparency. axe runs with animations off.
  - **Tables:** they scroll inside their own boxes.
- **Why:** A reset e-mail's link must lead somewhere, and both themes must meet the contrast rule.
- **Alternatives:** Deferring reset.

### K167 · Monthly report scope

- **Decision:**
  - **Schedule:** on the 1st at 09:00 UTC, `build_reports` sends `build_report` for every customer except the demo and suspended ones. The report covers the previous calendar month in the customer's time zone, and a manual run must name a finished month.
  - **Rows:** one per (customer, month). A month that has a PDF is not printed again.
  - **Print token:** 32 random bytes, with only its SHA-256 stored. It is valid for 60 minutes and deleted on first use.
  - **Retries:** printing makes at most 3 attempts, each with a new token, and then gives up with an error log.
- **Why:** Trial customers see reports too, and month boundaries match the panel's periods.
- **Alternatives:** Only active customers; UTC months; unlimited retries.

### K168 · Handing the print token to the browser

- **Decision:**
  - **Hand-off:** `build_report` writes the token to the shared hand-off volume (`reports/<id>/token`, 0600, `O_EXCL | O_NOFOLLOW`). The Dramatiq message carries only the report id and the attempt, and `print_report` deletes the file as soon as it reads it.
  - **PDF:** the browser writes `report.pdf` through a `.partial` file. It then sends `persist_report`, or `report_print_failed` on failure, to the `persist` queue, so the browser's Redis user needs no new rights.
  - **Errors:** `print_report` retries nothing and logs only the error type and Playwright's message.
- **Why:** The token never enters Redis, Dramatiq logs or Sentry's message arguments.
- **Alternatives:** Carrying the token in the message and masking it; a `reports` queue permission for the browser.

### K169 · The print browser context

- **Decision:**
  - **Browser:** `print_report` closes the thread's capture browser and opens a separate Chromium for printing, without the sentinel proxy and with a 60-second budget under the same watchdog.
  - **Network limits:** `--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE <print host>` and `--dns-prefetch-disable` stop every other name from resolving. Every request whose scheme, host, port and path prefix differ from the print base URL is aborted. WebSockets never connect.
  - **Print base URL:** `REPORT_PRINT_BASE_URL`, falling back to `APP_BASE_URL` (K185).
  - **Token:** added only to the page's navigation request as a header.
  - **PDF:** an A4 viewport (794×1123) in the light theme. Once the page sets `html[data-report-ready]`, `page.pdf` runs with A4, backgrounds and CSS page size.
- **Why:** A job without a proxy must not reach any other address, and pre-connections and DNS prefetch must be closed too.
- **Alternatives:** A proxied context; relying on request routing alone.

### K170 · The print page

- **Decision:**
  - **Access:** `web/src/proxy.ts` lets `/print/report/<id>` through without a cookie. It returns 401 without `X-Report-Token` and 404 for every other `/print` path, with `no-store` and `noindex`.
  - **Data:** the page fetches its data on the server through `API_ORIGIN` or `APP_BASE_URL`. In the cloud that path goes through the edge, and in self-hosting through Caddy's internal listener. A report id that is not a UUID makes no request.
  - **Failure:** without data the page says "This report is not available." and never sets the ready flag, so the print fails.
  - **Layout:** page 1 has the KPIs, the daily chart (no animation) and the status and channel cards. The following pages have the taken-down table, with headers repeated and no split rows, and the metric definitions.
- **Why:** The token stays out of the browser history, URLs and logs, and the panel and the report show the same numbers.
- **Alternatives:** A PDF library; giving web the api's direct address.

### K171 · Report CSV and e-mail

- **Decision:**
  - **CSV:** columns are `number, domain, category, status, first_seen_at, first_notice_at, taken_down_at, takedown_hours, channel`. Times are in the customer's time zone (ISO 8601, minutes), and durations are hours with one decimal. Formula cells are defused (K150).
  - **Storage:** files go to `reports/<customer_id>/<YYYY-MM>/report-<YYYY-MM>.{pdf,csv}` in the reports bucket.
  - **E-mail:** `monthly_report`, in English, goes once to active owners with the four headline numbers and a link.
  - **Demo:** the seed writes last month's row with a real CSV and no PDF.
  - **Manual runs:** the ops panel and `kalkan report build|list` share the same checks, and manual runs are audited.
- **Why:** The report uses the panel's definitions, and the demo's list and download work with real data.
- **Alternatives:** UTC times; a fake demo PDF.

### K172 · Alert rules and the local alert test

- **Decision:**
  - **Rules:** every alert in §23 is in `infra/compose/vm/prometheus/rules/kalkan.yml`, plus DeadLetters, AiBudgetExhausted, OutboxStuck, TargetDown and ExporterSourceDown.
  - **Thresholds:** written in the rules. Only the daily AI spend line is a variable (`ALERT_AI_DAILY_USD`), because it is money.
  - **Tests:** promtool tests in `prometheus/tests/` run in `make infra-check` and CI.
  - **Local trial:** the local `monitoring` profile runs the same rules every 5 seconds against the local targets, with Alertmanager sending to Mailpit. `make alert-test` fires AiDailySpendHigh end to end with a temporary `ai_calls` row.
- **Why:** promtool proves the rule logic, Alertmanager's tooling proves the mail path, and one real alarm proves the chain. A trigger for every alarm cannot be built without breaking things.
- **Alternatives:** Thresholds as variables (Prometheus rules do not expand them); firing every alarm locally.

### K173 · State metrics from the scheduler

- **Decision:** On every Prometheus scrape, the scheduler reads queue depths, open tasks, case and notice counts, and today's and this month's AI spend from Redis and the database (`kalkan.monitoring.exporter`, port 9400, application network only). If a source cannot be read, the scrape still succeeds without those gauges, and `exporter_source_up` is 0.
- **Why:** Worker counters reset on restart and miss what the ops panel does, while alarms need the system's state. The scheduler is a single, long-lived process that already has both connections.
- **Alternatives:** A collector in worker-default (collectors do not work in Dramatiq's multi-process mode); a separate exporter container; the ops process (its profile may be off).

### K174 · Cloud-side alarms (reference deployment)

- **Decision:** Cloud Monitoring holds the alarms the VM cannot see: a log-based policy on failed Cloud SQL automated backups (at most one notification an hour, auto-closing after 24 hours), and uptime checks on both public hosts through the edge.
- **Why:** Prometheus on the VM cannot see backups, and the VM's account should not get Cloud SQL read rights. Checking the edge from outside separates Cloudflare, DNS and Worker failures.
- **Alternatives:** Granting `cloudsql.viewer` to the VM; no backup alarm.

### K175 · Refreshing the demo on a server

- **Decision:** `kalkan seed --print-credentials` writes no file and prints the credentials once. With `ENV=prod`, a seed that writes a file is refused, the command asks for confirmation (`--yes` skips it), and no print token is made. If any demo domain appears in a real customer's case or match, nothing is written, because the `domains` table is shared. The command runs with `exec` in the `ops` container, which has the TOTP key and storage access, and `exec` output does not reach the log driver.
- **Why:** Demo credentials must stay out of the repository and the logs, and real tenants' data is never touched.
- **Alternatives:** A separate "demo refresh" command; storing the credentials as a secret.

### K176 · Local load check

- **Decision:** `make load` runs the CT handler and worker-default's actors in one process against real Postgres and Redis. DNS, RDAP, candidate sites, the browser and Claude are delayed fakes, with the same thread counts as production. At 10,000 matches an hour, admission, enrichment and the pre-check keep up. The bottleneck is the browser, at about 450 jobs an hour.
- **Why:** Real proxies and Claude cost money, and the browser's resource use is known from its own tests. This check measures queue behavior.
- **Alternatives:** The full stack in containers with a real browser; a load tool such as Locust (the pipeline is queues, not HTTP).

---

## Portfolio-era decisions

### K177 · One squashed initial migration

- **Decision:** The portfolio version starts from a single Alembic revision, `0001_initial_schema`, which contains the final schema: RLS policies, role grants, resolvers and default settings. Older decisions that mention later migrations (for example K110, K111, K132 and K136) now point to columns and constraints that exist in `0001`. New schema changes are new revisions on top of it, and an existing migration is never edited.
- **Why:** One readable migration documents the final schema in one place. A reader does not have to replay a long chain of revisions, some of which only changed columns that later revisions changed again.
- **Alternatives:** Keeping the full chain of revisions.

### K178 · Customer statuses

- **Decision:** A customer is `trial`, `active` or `suspended`, set by an operator in the ops panel.
  - **Gate:** gate 3 stops sending for a `suspended` customer.
  - **Read-only:** a workspace is read-only only for the demo and for suspended customers (K150, K160).
- **Why:** Three states cover what an operator needs: a customer trying the service, a running one and one that is paused. The status has one owner, the operator, so no background process changes it.
- **Alternatives:** More statuses driven by automated rules, which would add states to every query and screen.

### K179 · Fintech demo data

- **Decision:** The demo customer is Mintera Bank, a fictional digital bank:
  - slug `mintera-demo`, keyword `mintera`, variants `mint era` and `mint-era`;
  - official domains `mintera.com` and `mintera.pl`;
  - market PL, languages pl and en, time zone Europe/Warsaw.

  Fixture sites, test names and examples use banking scenarios: the clone asks to verify a card, and the lure page sends visitors to the clone and to `quick-loans.example`. Enum values stored in the database stay as they are (for example `mirror`, `affiliate_or_review`, `deposit_cta`), and only user-facing labels changed. The seed keeps every number the tests and the design rely on (K49).
- **Why:** Banking is a familiar phishing target for any reader, and the brand is clearly fictional. Keeping enum values and seed numbers meant no schema change and no test rewrite.
- **Alternatives:** Renaming enum values (a schema migration for cosmetic reasons); a generic placeholder brand without an industry.

### K180 · Filesystem storage keeps `gs://` as a logical key scheme

- **Decision:** `STORAGE_BACKEND=filesystem` stores objects under `STORAGE_ROOT` (`storage/filesystem.py`) with the same guarantees as the GCS backend:
  - **Write-once:** bytes go to a temporary file, are fsynced and are published with `os.link`, which fails atomically if the name exists. This is the filesystem's equivalent of `ifGenerationMatch=0`.
  - **SHA-256:** returned on write and recorded in a sidecar metadata file. If that file could not be written (a full disk, a crash), the next hash lookup computes it from the object's bytes and writes it, so a retried capture is not stuck on its own earlier object.
  - **Temporary files:** a crash can leave them under `.tmp/`; files older than an hour are removed when a process first opens the store.
  - **Safe paths:** bucket and path checks refuse traversal and symlink escapes.
  - **Deletes:** only under `demo/`.

  Object keys keep the `gs://<bucket>/<path>` form. The scheme names a logical object, not Google Cloud. The local stack and the cloud reference keep `STORAGE_BACKEND=gcs` (fake-gcs locally), and self-hosted servers use the filesystem.
- **Why:** Keys are stored in several columns, built in many modules and parsed by one function. Keeping the format means no caller, migration or API change, and moving between backends is a copy of the bucket trees. A neutral scheme would have touched every stored key for no functional gain.
- **Alternatives:** A neutral `obj://` scheme with a data migration; MinIO or another S3-compatible server (another service to run).

### K181 · SMTP is the default e-mail provider

- **Decision:** `EMAIL_PROVIDER` selects the provider: `smtp` (the default everywhere) or `postmark`.
  - **SMTP URLs:** `smtp://` (port 25, no TLS), `smtp+starttls://` (587) and `smtps://` (465), with an optional percent-encoded `user:password@`.
  - **Credentials:** a user or password over plain `smtp://` is refused at start-up, so a password is never sent in the clear. `SMTP_URL` is a secret setting, and the password is kept out of `repr` and error messages.
  - **Plain SMTP in production:** with `ENV=prod`, plain `smtp://` is accepted only for loopback, a private or link-local address or a single-label host (a compose service such as `postfix`), unless `SMTP_ALLOW_PLAINTEXT=true`. Notices and password-reset links would otherwise cross the internet in clear text, with no STARTTLS to downgrade-protect.
  - **TLS:** certificates and host names are verified.
  - **Postmark and local runs:** `ENV=local` refuses `postmark`, so a developer machine can reach only Mailpit.
  - **Defaults per deployment:** the cloud reference sets `EMAIL_PROVIDER=postmark` in its `app.env`, and self-hosted servers default to SMTP.
- **Why:** Every self-hoster has some SMTP server, and few have a Postmark account. Plain `smtp://` remains useful for a relay on the same host or network (Mailpit, a local MTA), but any credentials mean the server is remote. Refusing Postmark locally keeps K139's guarantee that local runs cannot e-mail a real recipient.
- **Alternatives:** Postmark only (a paid account to run Kalkan); allowing AUTH over plain SMTP.
- **Trade-off:** Reply and bounce tracking still needs Postmark's inbound and bounce webhooks (K146).

### K182 · A 5xx answer to RCPT TO is a permanent failure

- **Decision:** If every refused recipient got a 5xx code, `SmtpProvider` raises `PermanentEmailError`, which marks the notice `failed`. A 4xx refusal, an AUTH failure and connection or protocol errors raise `TemporaryEmailError`, so the job retries and the notice stays `queued`.
- **Why:** This follows Postmark's split in K139. A 5xx to RCPT TO means the mailbox will not accept the message, and retrying only delays the operator's task. Login and connection problems are account or network issues that a retry, or a configuration fix, resolves.
- **Alternatives:** Treating every SMTP error as temporary, which would retry undeliverable notices until the job gives up; treating AUTH failures as permanent, which would mark notices failed because of a typo in `SMTP_URL`.

### K183 · An egress proxy for the browser on self-hosted servers

- **Decision:** In self-hosting, `worker-browser` sits on an internal Docker network with Redis and `egress-proxy`, and has no other route out. The proxy (`python -m kalkan.net.egress_proxy`) is configured as `PROXY_MAP='{"XX":"http://egress-proxy:8888"}'` with `PROXY_DEFAULT_COUNTRY=XX`. It:
  - resolves each target once with the same resolver and `is_public` test as `net/safe_http.py`, and answers 403 for loopback, RFC 1918, link-local and metadata, CGNAT, ULA and their IPv4-mapped forms;
  - connects to the checked address, never to the name, which closes DNS rebinding;
  - allows only ports 80 and 443, for CONNECT and plain-http requests alike;
  - caps the request head's size and arrival time, the number of clients and idle time;
  - handles one request per client connection and logs no paths or queries;
  - refuses a request head with a bare CR or LF or a NUL (400), so no header can be smuggled to the site;
  - shuts sockets down before closing them and waits for both relay threads, so no thread outlives its connection.

  It has no authentication, so it must stay on the internal network. `SSRF_ALLOW_CIDRS` extends it for fixtures and tests and is refused with `ENV=prod`.
- **Why:** The cloud reference relies on a host firewall (K82) and residential proxies (K93), which a self-hosted server does not have. Without them, Chromium could reach the server's own services or the metadata endpoint. Reusing `safe_http`'s rules keeps one definition of "public address", and the existing fail-closed `PROXY_MAP` path (K94) needed no browser changes.
- **Alternatives:** tinyproxy with filters (it cannot check resolved addresses); host iptables rules (documented as optional defense in depth, but they need root and differ per host).
- **Trade-off:** Pages leave from the server's own IP, with no exit per country.

### K184 · AI is optional

- **Decision:** `Settings.use_ai` is `AI_ENABLED` when set, otherwise whether `ANTHROPIC_API_KEY` is set. Every sending process sets it once at start-up (`ai.queue.set_ai_enabled`). Without AI, messages for AI actors go to stand-ins in worker-default (`workers/without_ai.py`), with the same arguments, instead of the `ai` queue:

  | AI actor | Stand-in without AI |
  | --- | --- |
  | `ai_triage`, `ai_confirm` | The case goes to the review queue with reason `ai_disabled`. |
  | `ai_notice_text` | The fixed explanation paragraph (K138). |
  | `ai_classify_reply` | The reply is left to an operator. |

  The flag is process state, so tests and tools that never boot a process keep sending to worker-ai.
- **Why:** The pipeline must finish without a paid account, and nothing may wait on an `ai` queue that nobody consumes. Routing at the sending side reuses the existing fallbacks for an unavailable AI (`ai_unavailable`, the fixed paragraph, the operator task), so behavior without AI matches the AI-failure path that was already tested. The decision rules still decide, and only the input changes.
- **Alternatives:** A worker-ai that answers "unavailable" (one more service to run); decision rules without a triage verdict (a second rule set).

### K185 · One-command demo and the `print` network

- **Decision:**
  - **`make demo`:** needs only Docker and make. It creates `.env` from `.env.example` when missing (never overwriting it), builds the images and starts the stack with the `demo` profile (a production build of the web panel). It then runs the one-off `demo-setup` (`kalkan demo setup`), which seeds Mintera Bank, renews the ops operator `operator@kalkan.local` and prints every login.
  - **Credentials:** they go to `.local/demo-credentials.json` (`DEMO_CREDENTIALS_FILE`). `demo-setup` runs as the host user so it can write there.
  - **Companion targets:** `make demo-code EMAIL=…` prints a current TOTP code, `demo-info` repeats the logins, `demo-browser` adds the browser with dev-proxy and the fixture sites, `demo-down` stops the stack, and `demo-reset` deletes the volumes after confirmation.
  - **Ports and overrides:** every published port has a variable (`API_PORT`, `WEB_PORT`, …), and `COMPOSE_OVERRIDE` layers an extra compose file, for example for other subnets. The demo commands refuse `ENV=prod`.
  - **Report printing:** `REPORT_PRINT_BASE_URL` sets where worker-browser opens the print page when that differs from the public address (falling back to `APP_BASE_URL`). In the demo and in self-hosting it is `http://web:3000`, reached over a separate internal `print` network that only `worker-browser` and `web` join.
- **Why:**
  - Reviewers should see the full product in one command, with TOTP still mandatory.
  - The public address is `localhost` in the demo, which inside the container is the container itself, and an HTTPS domain on a server.
  - Routing the print through dev-proxy or egress-proxy would mean letting a proxy reach compose services, which is worse.
  - The trade-off is that a compromised Chromium can reach the panel's server, while Redis and the proxies stay off that network. In the local demo the panel also forwards `/api/*` to the api (its build-time rewrite), so Chromium could call the API as an anonymous client. The self-hosted panel is built without the rewrite.
- **Alternatives:** Host tools (uv, node) for the demo; running the seed on the host; printing through the public URL; turning TOTP off for the demo.

### K186 · Caddy is the self-hosted edge and injects `X-Edge-Auth`

- **Decision:** On a self-hosted server, Caddy (`deploy/selfhost/Caddyfile`) is the only service with public ports. It does what the Cloudflare Worker does in the cloud (K84):
  - **Certificates:** automatic HTTPS for `APP_DOMAIN` and `EVIDENCE_DOMAIN`.
  - **Routing:** `/api/*` and `/healthz` go to `api`, and the rest of the app domain to `web`. The whole evidence domain goes to `api`.
  - **Edge header:** it adds `X-Edge-Auth: <EDGE_SHARED_SECRET>` to every forwarded request, replacing any value the client sent.
  - **Client address:** it sets `X-Forwarded-For` from the client address and ignores incoming values.
  - **Admin endpoint:** off.
  - **Internal listener:** `:8080`, API paths only, lets the panel's server read report data with the header without knowing the secret (K170).

  The api and web run with `ENV=prod`, so requests without the header are refused. The ops panel listens on 127.0.0.1 only and is reached through an SSH tunnel.
- **Why:** The same `ENV=prod` code path as the cloud: the edge check, secure cookies, and no test settings. Only requests that passed the edge are served, even if a port is published by mistake. Caddy obtains certificates itself and needs no account.
- **Alternatives:** Turning the edge check off for self-hosting (a second security mode to test); Traefik or nginx with certbot (more configuration).

### K187 · The cloud deployment is an optional reference

- **Decision:** The Google Cloud and Cloudflare setup (`infra/terraform`, `infra/cloudflare`, `infra/compose/docker-compose.vm.yml`, `infra/compose/vm/`, `infra/vm/`) is kept as an optional reference deployment, with placeholders (`my-gcp-project`, `example.com`) instead of real ids. The repository ships no deploy workflow: `infra/terraform/README.md` lists the steps, and K76's WIF condition still trusts exactly one workflow file for anyone who adds one. CI checks the reference files (`terraform validate`, TFLint, compose `config`, shellcheck, promtool) without any cloud credentials.
- **Why:** The reference shows how the system runs at production scale (Cloud Run, Cloud SQL, one VM with firewall rules, an edge Worker), but a deploy workflow cannot run without someone's cloud account and would only fail in a fork. The self-hosted setup (K180 to K186) is the supported way to run Kalkan.
- **Alternatives:** Removing the cloud files (losing the hardened reference); keeping a deploy workflow that cannot run.

### K188 · Panel redesign: Red Hat type and "Plain sight" tokens

- **Decision:** The customer panel and the monthly report use Red Hat Display, Red Hat Text and Red Hat Mono through `next/font/google`; the `geist` package is removed. `next/font` downloads the files once at build time and serves them from the panel's own origin, so browsers never call Google. The visual system is "Plain sight" (architecture.md §18): cool slate neutrals, one navy accent kept apart from the reserved status inks, an icon plus a word for every status, one panel style without shadows, no gradients and no fill under chart lines. `html` sets `text-rendering: geometricPrecision`, because Chromium on Linux (and so the PDF printer) rounds Red Hat Text's word spaces away at 10 to 13 px with hinting on.
- **Why:** Customers found the old panel generic and hard to read. A bank security manager should see what every section and number means without knowing our terms. The Red Hat superfamily gives headings, body and code faces with shared proportions.
- **Alternatives:** Keeping `geist` (fully offline builds, but the generic look); bundling the font files in the repository or from `@fontsource` packages (offline builds, more files to keep current).
