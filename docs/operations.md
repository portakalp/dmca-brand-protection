# Operations

This is the runbook for the people who run Kalkan: the ops panel, the alarms, logs, upgrades and rollbacks, incidents, restores and rehearsals. Installing Kalkan is in [self-hosting.md](self-hosting.md).

Commands assume a self-hosted server ([deploy/selfhost](../deploy/selfhost/README.md)) and run from `deploy/selfhost` on that server. On the local stack, use `docker compose --env-file .env -f infra/compose/docker-compose.yml` from the repository root instead of `docker compose`. The optional Google Cloud reference deployment runs the same containers on a VM; where it differs, this page says so and points to `infra/`.

Two shortcuts used below:

```sh
# psql as the database owner (full rights: be careful)
docker compose exec postgres psql -U kalkan_owner -d kalkan
# the operator CLI (writes to audit_log with actor "cli")
docker compose exec ops kalkan --help
```

## Ops panel

The ops panel is the operator's internal tool ([architecture.md §19](architecture.md)). It listens on `127.0.0.1:8081` on the server. Reach it through an SSH tunnel (`ssh -N -L 8081:127.0.0.1:8081 you@server`) and open http://localhost:8081.

**Signing in.** Password, then a TOTP code. Accounts come from `kalkan staff create` ([self-hosting.md](self-hosting.md#install)).

- 5 wrong passwords or codes within 15 minutes for the same e-mail address or IP lock it for 15 minutes. Each further batch of failures that day doubles the lock, up to 24 hours. Everything that comes through the SSH tunnel shares one source address, so an IP lock locks the tunnel: wait it out.
- A session ends after 12 hours without activity or 7 days in total.
- Every change made in the panel is written to `audit_log`:

  ```sql
  SELECT at, actor, action, target FROM audit_log ORDER BY at DESC LIMIT 20;
  ```

The pages:

| Page | What it is for |
| --- | --- |
| Review | Cases the decision engine could not settle; the keyboard-driven queue below |
| Cases | Every case with its evidence, signals, verdicts, decisions, notices and timeline |
| Outbox | Every notice, real or dry-run, with the reason it is blocked |
| Tasks | Work for a human: web forms, retractions, escalations, replies to read, bounces |
| Replies | Inbound e-mails that matched no case, and the classification of linked ones |
| Labelling | Label cases for the AI evaluation set (`kalkan eval export`) |
| Customers | Customers, sending flags, letter of authorization (LOA), brands, official domains, whitelist import, users and invitations, monthly reports |
| Settings | Global switches and thresholds, the kill switch, blocked notices |
| Failed jobs | Jobs whose retries ran out (`job_failed`), with a requeue button |
| System | Queue depths, outbox state, this month's AI spend and browser traffic |

### Review queue

The queue holds the cases in `in_review` with an open review task, oldest case number first. Demo cases are excluded. Each case shows:

- the desktop and mobile screenshots next to the official site's references;
- the signals: visual and text similarity, favicon match, password and card fields, an external form action, redirects, official infrastructure, a Web Risk listing;
- the triage and confirmation verdicts with their evidence, when AI is on;
- the rule's suggestion and the reason the case needs review (for example `ai_disabled` or "Auto-approve disabled").

Work it from the keyboard:

| Keys | Action | Result |
| --- | --- | --- |
| `A` then `P` | Approve, phishing basis | `approved`; the first wave of notices is planned |
| `A` then `T` | Approve, trademark basis | `approved` |
| `V` | Verify only | `verified`; no notice is sent |
| `R` then `1`–`6` | Reject with a category: 1 phishing clone, 2 brand impersonation, 3 affiliate or review, 4 parked or empty, 5 unrelated, 6 unclear | `dismissed`; a light check after 30 days |
| `W` | Watch | Checked again in 7 days |
| `C` | Recapture | Back to `detected`: evidence, signals, AI and decision run again |
| `N` | Skip for this pass | |
| `Esc` | Cancel a half-typed sequence | |

"Save as a label too" is on by default: the decision is also stored as a label for the evaluation set. Every decision lands in `decisions`, the case timeline and `audit_log`:

```sql
SELECT d.created_at, c.number, d.outcome, d.rule, d.decided_by
FROM decisions d JOIN cases c ON c.id = d.case_id
ORDER BY d.created_at DESC LIMIT 20;
```

**Auto-approve.** `auto_approve_enabled` (Settings) is off by default. Turn it on only after an evaluation report shows that the cases rule R1 sends to confirmation are right at least 98% of the time ([architecture.md §14](architecture.md)). While it is off, cases that both models call phishing come to review with the reason "Auto-approve disabled" and take one `A P`.

### Outbox and tasks

An approved case plans its first wave of notices: the registrar, the hosting provider (unless the site is behind a CDN) and a Google Safe Browsing report as a manual task. Each notice then passes the sending gates in this order ([architecture.md §15](architecture.md)):

1. `monitor_only`: the customer is watch-only (no preview either);
2. `sending_disabled`: the customer's sending is off;
3. `suspended`: the customer is suspended;
4. `dry_run`: global `NOTICES_DRY_RUN`, the customer's `dry_run` or `is_test`; the preview goes to `DRY_RUN_REDIRECT_EMAIL`;
5. `kill_switch`: the global `sending_enabled` setting is off;
6. `daily_limit`: the channel's real notices for the UTC day reached the limit in Settings;
7. `no_loa`: the customer has no letter of authorization on file;
8. `legal_review`: the template's legal text is not approved yet.

- **Outbox** lists every notice. Blocked ones show their gate, and previews are marked "dry run".
- **Tasks** holds what needs a person: web-form channels (`manual_channel`), retractions, escalations, `needs_info` (the abuse desk asks for more), `reply` (a reply to read), `bounce`. The text is ready with copy buttons. Press **Submitted** after filling in a form, or **Done** for the rest. Filter with `/tasks?kind=needs_info`. "[DRY RUN]" tasks belong to previews and need no action.

Why is a case's notice blocked?

```sql
SELECT c.number, n.channel, n.status, n.blocked_reason, n.dry_run, n.recipient
FROM notices n JOIN cases c ON c.id = n.case_id
ORDER BY n.updated_at DESC LIMIT 30;
```

**Registry contacts** are never taken from RDAP. Look up the registry's abuse address on its website (ICANN requires registries to publish it) and enter it:

```sh
docker compose exec ops kalkan contact set registry online --email abuse@registry.example --language en
```

`kalkan contact set host AS<number>` and `kalkan contact set registrar <IANA id>` override what RDAP returned.

**Follow-up.** Notified cases are checked every 6 hours. When the HTTP check sees the site down, the browser confirms it. A site that stays down for 72 hours becomes `taken_down` and is watched daily for 30 days, then archived. If it comes back, the case reopens and a new round of notices goes out. 48 hours after the first wave a live phishing site escalates to the registry, and a trademark case opens a task. At 96 hours the operator gets a task ([architecture.md §16](architecture.md)).

### Replies

With Postmark's inbound webhook configured, replies to notices arrive here.

- A reply that matches a case is classified (`removed`, `needs_info`, `not_our_customer`, `auto_reply`, `other`) and acted on. The case timeline shows `reply_received`.
- An unmatched reply waits in the list. Type the case number and press **Link**.
- A wrong class can be corrected from the list; the action runs again.

### Customers

Onboarding a customer: create it (status `trial`, sending off, dry-run on), add brands with keywords and the target market, official domains (the primary one also fetches the favicon and reference screenshots), import the whitelist (CSV with `pattern,kind,note`; preview first), then invite the owner. The flags on the customer page each need the confirmation box and are audited:

| Flag | Effect |
| --- | --- |
| `monitor_only` | Watch only: no notice and no preview, ever |
| `sending_enabled` | The customer's own send switch |
| `dry_run` | Previews only, to `DRY_RUN_REDIRECT_EMAIL` |
| `is_test` | A permanent dry-run, for rehearsal tenants |

Turning a flag to its open value re-plans the customer's blocked cases. The **Monthly reports** section builds a report by hand; the scheduler builds last month's report on the 1st at 09:00 UTC for every customer except demo and suspended ones ([architecture.md §21](architecture.md)).

### Settings and the kill switch

- `sending_enabled` is the global send switch, and the kill switch. Off stops every real notice at once. Previews continue. Turning it on re-plans the blocked cases of every customer.
- `auto_approve_enabled` and the thresholds (`triage_min_confidence`, `confirm_min_confidence`, `visual_similarity_min`, `dismiss_min_confidence`) feed the decision rules.
- `channel_daily_limits` caps the real notices per channel and UTC day.
- **Re-check blocked cases** re-plans everything blocked, for example after a legal text was approved.
- `NOTICES_DRY_RUN` and `DRY_RUN_REDIRECT_EMAIL` are environment variables and only shown here. Change them in `.env`, then run `docker compose up -d`.

### Dry-run and going live

A fresh installation sends nothing real. Every layer starts closed. Open them in this order, and check the Outbox after each step:

1. Mail works: the From address is accepted by your SMTP server or approved in Postmark, with SPF, DKIM and (for Postmark) Return-Path records in place. Send yourself an invitation to check.
2. The legal texts in `backend/src/kalkan/notices/legal/` are reviewed. Remove the first line, `LEGAL_REVIEW_REQUIRED`, from each approved file, rebuild (`docker compose up -d --build`), then press **Re-check blocked cases**.
3. The customer's LOA is entered and the customer's `sending_enabled` is on. Previews now reach `DRY_RUN_REDIRECT_EMAIL`. Gate 2 comes before the dry-run gate.
4. Turn on the global `sending_enabled` (Settings).
5. Turn off the customer's `dry_run`. Pending cases are re-planned. Preview rows stay as they were.
6. Set `NOTICES_DRY_RUN=false` in `.env` and run `docker compose up -d`.

**Retractions.** When a domain that already received notices is added to the whitelist, either by the customer ("This is our site") or by an operator, the same recipients get a retraction. Notices sent through web forms, and retractions that were blocked, become tasks.

## Alarms

The alert rules are in `infra/compose/vm/prometheus/rules/kalkan.yml`, with unit tests in `infra/compose/vm/prometheus/tests/` (`make infra-check`). The self-hosted `monitoring` profile and the reference VM use the same rules and the same Grafana dashboards (Queues, Pipeline, Channels, Cost). Alertmanager e-mails `OPS_ALERT_EMAIL`, repeats an alarm every 4 hours while it fires, and sends "RESOLVED" when it clears.

**Test the mail path** (it is the same for every rule). The alarm arrives within seconds and resolves itself after 5 minutes:

```sh
docker compose exec alertmanager amtool alert add alertname=AlarmTest severity=warning \
  summary="test from the runbook" --alertmanager.url=http://127.0.0.1:9093 \
  --end="$(date -u -d '+5 min' +%Y-%m-%dT%H:%M:%SZ)"
```

Locally, `make up` and then `make alert-test` (which starts the `monitoring` profile) fire `AiDailySpendHigh` and wait for its e-mail in Mailpit (http://localhost:8025). Prometheus is at http://localhost:9090/alerts, Alertmanager at http://localhost:9093.

Default thresholds: 100 messages for 30 minutes per queue, 10 minutes for CT, at least 10 captures and 50% failures for captures, 80% for the disk, 24 hours for tasks. To change one, change the rule and its test together.

### QueueBacklog

- **Meaning.** A queue has held more than 100 messages for 30 minutes without shrinking.
- **Trigger once.** Not recommended on a live server: it needs a stopped worker and a real backlog. The promtool tests cover the rule.
- **First response.** In Grafana's Queues dashboard, find the queue. For `capture`, check the browser (see [BrowserUnavailable](#browserunavailable) and [CaptureErrorRate](#captureerrorrate)). For `ai`, see [Claude outage](#claude-outage). For the others, read `docker compose logs --tail=200 worker-default`. A CT burst from a new, broad keyword can also fill the queues: narrow the brand's keywords.

### DeadLetters

- **Meaning.** A job used up its retries and went to a dead-letter queue.
- **Trigger once.** It needs a broken job; not recommended.
- **First response.** Find the actor's error in the worker log. The case shows `job_failed` and is listed under **Failed jobs**. Fix the cause, then requeue from there. The reconciler also re-queues stuck stages on its own.

### NoCtEvents

- **Meaning.** No certificate updates from the CT stream for 10 minutes. It also fires permanently while the `ct` profile is off: turn `ct` on, or silence the alarm in Alertmanager.
- **Trigger once.** `docker compose stop certstream`, wait 11 minutes, `docker compose start certstream`. TargetDown follows on the reference VM.
- **First response.** `docker compose logs --tail=100 certstream ct-consumer`, then restart `certstream`.

### AiDailySpendHigh

- **Meaning.** Today's AI spend passed `ALERT_AI_DAILY_USD` (default 5 USD).
- **Trigger once.** Insert a fake call in psql, then delete it when the e-mail arrives. It counts 6 USD towards the month until you delete it:

  ```sql
  INSERT INTO ai_calls (id, purpose, model, prompt_version, cost_usd, error)
  VALUES (gen_random_uuid(), 'eval', 'alarm-test', 'alarm-test', 6, 'runbook');
  DELETE FROM ai_calls WHERE model = 'alarm-test';
  ```

- **First response.** [Budget alarm](#budget-alarm).

### AiBudgetExhausted

- **Meaning.** The month's AI spend reached `AI_MONTHLY_BUDGET_USD`. New cases go to review without AI.
- **Trigger once.** Not recommended: it stops AI calls.
- **First response.** [Budget alarm](#budget-alarm).

### CaptureErrorRate

- **Meaning.** More than half of the browser captures in the last hour failed (with at least 10 attempts).
- **Trigger once.** No safe way to trigger it by hand; the promtool tests cover the rule.
- **First response.** Read `docker compose logs --tail=200 worker-browser egress-proxy`. Look for a proxy problem (`egress.refused`, connection errors; see [Proxy or egress problems](#proxy-or-egress-problems)) or a group of sites that time out.

### ProxyMissing

- **Meaning.** Captures wait because `PROXY_MAP` has no proxy for a country (`capture.deferred`, `reason=no_proxy`).
- **Trigger once.** Not recommended.
- **First response.** On a self-hosted server the map is fixed in `docker-compose.yml` (`XX` → `egress-proxy`). Check that `worker-browser` runs with that value. In the reference deployment, check the `proxy-map` secret.

### ProxyDailyLimit

- **Meaning.** The day's estimated browser traffic passed `PROXY_DAILY_GB_LIMIT`. Captures resume the next UTC day.
- **Trigger once.** Not recommended.
- **First response.** The System page shows the month's traffic. If it is expected growth, raise `PROXY_DAILY_GB_LIMIT` in `.env`. If not, look for a site that serves huge pages, or a burst of candidates.

### BrowserUnavailable

- **Meaning.** `worker-browser` cannot start Chromium, so captures wait.
- **Trigger once.** Not recommended.
- **First response.** `docker compose logs --tail=200 worker-browser`. `No usable sandbox!` means the host blocks user namespaces: see the AppArmor note in [deploy/selfhost/README.md](../deploy/selfhost/README.md#the-browser-and-its-egress-proxy). Never add `--no-sandbox`.

### DiskAlmostFull

- **Meaning.** The host's disk is 80% full.
- **Trigger once.** Not recommended; check with `df -h /`.
- **First response.** `docker system prune` removes unused images and build cache. Container logs are capped (5 × 10 MB per container) and Prometheus keeps at most 2 GB. Evidence in the `objects` volume only grows: move old backups off the host, or grow the disk.

### ManualTaskWaiting

- **Meaning.** A task other than review has been open for more than 24 hours. Dry-run "[DRY RUN]" form tasks count too.
- **Trigger once.** No safe way to trigger it by hand; the promtool tests cover the rule.
- **First response.** Work through the ops **Tasks** page.

### OutboxStuck

- **Meaning.** Rows in the transactional outbox could not be dispatched to the queues for 10 minutes.
- **Trigger once.** No safe way to trigger it by hand; the promtool tests cover the rule.
- **First response.** The System page shows the outbox. Read `docker compose logs --tail=200 scheduler` and check that Redis is healthy (`docker compose ps redis`).

### TargetDown

- **Meaning.** Prometheus could not scrape a target for 10 minutes.
- **Trigger once.** `docker compose stop worker-default` for 11 minutes, then start it again.
- **First response.** Restart the service named in the alarm. On a self-hosted server, services of profiles that are off have no target and stay quiet.

### ExporterSourceDown

- **Meaning.** The scheduler's metrics exporter cannot read Redis or the database.
- **Trigger once.** No safe way to trigger it by hand; the promtool tests cover the rule.
- **First response.** `docker compose logs --tail=100 scheduler redis postgres`.

### Reference deployment only

The cloud reference also has Cloud Monitoring alerts, defined in `infra/terraform/monitoring.tf`: a failed or skipped nightly Cloud SQL backup, and uptime checks for the app, evidence and API hosts. A self-hosted server has neither. Watch your backup job's exit status, and use any external uptime checker against `https://APP_DOMAIN/healthz`.

## Logs

Every Kalkan process writes JSON lines to stdout: `event`, `level`, `logger`, `ts`, `service` and the event's own fields. Secrets, evidence tokens and URL paths that carry them are redacted before they are written. Docker's `local` driver keeps 5 × 10 MB per container.

```sh
docker compose logs --tail=200 -f worker-default
docker compose logs --since 30m worker-default worker-browser | grep '"report\.'
docker compose logs --since 1h api worker-default | grep '"level": "error"'
```

Events worth knowing:

| Event | Meaning |
| --- | --- |
| `capture.deferred` | A capture waits: `no_proxy`, `proxy_daily_limit` or `browser_unavailable` |
| `capture.browser_launch_failed` | Chromium did not start (sandbox, memory) |
| `egress.refused` | `egress-proxy` refused a host, with the reason (private address, port) |
| `notices.failed` | A notice could not be sent (SMTP or Postmark error) |
| `report.build_refused` | A monthly report was refused: demo or suspended customer, or the month has not ended |
| `report.print_failed`, `report.print_given_up` | `worker-browser` could not print the report; it retries 3 times |

The reference deployment sends the same lines to Cloud Logging (the VM's `gcplogs` driver and Cloud Run); the Terraform in `infra/terraform` sets it up.

## Rollback

**Upgrade** (repeated from [self-hosting.md](self-hosting.md#upgrades)): back up, `git pull`, `docker compose up -d --build`. The `migrate` service applies new migrations before the application starts.

**Roll back the code.** Migrations are written so that the previous release still works on the new schema. Going back one release is therefore:

```sh
umask 077 && docker compose exec -T postgres pg_dump -U postgres -Fc kalkan > /var/backups/kalkan/kalkan-before-rollback.dump
git checkout <previous tag or commit>
docker compose up -d --build
```

`migrate` then runs `alembic upgrade head` of the old code, which changes nothing on a newer schema.

**Roll back the schema** only when the old code really cannot run on the new schema. Do it before you check out the old code, while the new image, which knows the newer migrations, is still in place:

```sh
docker compose run --rm migrate alembic history          # find the old release's revision
docker compose run --rm migrate alembic downgrade <revision>
```

Then check out and rebuild as above. A downgrade can drop columns and their data: take the dump first.

In the reference deployment, a rollback is the same deploy with an earlier image tag and `deploy.sh <tag> --skip-migrations`; schema downgrades are manual there too ([infra/terraform/README.md](../infra/terraform/README.md#deploying)).

## Incidents

For every incident, note when it started, how long it lasted and which customers it touched. Afterwards, write down the root cause and every step that did not leave an `audit_log` row.

### Wrong notice

A notice went to an abuse desk about the customer's own site, a partner or an innocent site.

1. **Stop.** If similar cases are at risk, turn off the global `sending_enabled` (Settings). If only one customer is affected, turn off that customer's `sending_enabled`.
2. **Retract.** Whitelist the domain: the customer's owner presses "This is our site" in the panel, or an operator adds it in ops. The whitelist sends a retraction to the same recipients. Retractions are blocked only by the global dry-run, the kill switch (global `sending_enabled` off) and the legal-review gate. If you turned the global `sending_enabled` off, the retraction waits too: turn it back on briefly until the retraction shows as sent, or send it by hand from the task.
3. **Web forms.** For notices sent through a form (Google Safe Browsing, some registrars) a retraction task opens. Use the form's "incorrect report" route, then press **Submitted**.
4. **Check.** The original notices should be `retracted` and the retractions `sent`:

   ```sql
   SELECT n.channel, n.template, n.status, n.sent_at, n.recipient
   FROM notices n JOIN cases c ON c.id = n.case_id
   WHERE c.number = 'KLK-000123' ORDER BY n.created_at;
   ```

5. **Root cause.** The case page shows its decisions and verdicts. Was a whitelist entry missing (ask the customer for their partner list), was the AI wrong (label the case so the next evaluation includes it), or was it an operator's approval?
6. Tell the customer briefly what happened, then turn sending back on.

### Kill switch and emergency stop

- **Stop real notices now:** ops Settings → `sending_enabled` off. Approved cases wait with `blocked_reason=kill_switch`. Previews continue. Turning it back on re-plans them.
- **If the panel is down**, the same switch in SQL. This path writes no `audit_log` row:

  ```sql
  INSERT INTO settings (key, value, updated_by) VALUES ('sending_enabled', 'false', 'runbook')
  ON CONFLICT (key) DO UPDATE SET value = 'false', updated_at = now(), updated_by = 'runbook';
  ```

- **Stop the pipeline:** `docker compose stop worker-default scheduler` (and `worker-ai`, `worker-browser` if running). Start them again with `docker compose start ...`. Redis has no persistence, so a full `docker compose stop` loses the queued jobs. No work is lost, because the state is in Postgres and the reconciler and the outbox queue the jobs again.

A container you stopped by hand stays stopped across a reboot, but the next `docker compose up -d` (an upgrade, a settings change) starts it again. To keep sending off, use the switch, not a stopped container.

### Claude outage

Signs: `APIStatusError`, `overloaded` or timeouts in `docker compose logs worker-ai`, and the `ai` queue grows (QueueBacklog).

- The client retries, and messages wait in the queue until the API is back. Usually nothing needs doing. Failed calls cost nothing.
- Status: https://status.anthropic.com
- If it lasts for hours, new cases wait in `detected`. Handle urgent ones by hand: with AI unavailable, a case can still be reviewed and approved in ops.
- To run without AI for a while, set `AI_ENABLED=false` in `.env` and run `docker compose up -d`. New cases go straight to review.

### Proxy or egress problems

Signs: CaptureErrorRate, the `capture` queue grows, connection errors in `worker-browser`.

- Self-hosted: `docker compose ps egress-proxy`, then `docker compose logs --tail=100 egress-proxy`. A crowd of `egress.busy` lines means the proxy hit its connection limit. `egress.refused` for public sites points at DNS (the proxy resolves every host itself).
- Failed captures use up their retries and end as `job_failed`. The reconciler tries again 2 hours later. For a long outage, stop `worker-browser` instead: the queue grows, but no retries are spent. Start it when the cause is fixed.
- Reference deployment: check the residential proxy provider's status page. To switch provider, add a new version of the `proxy-map` secret and restart the stack.

### Mail provider outage

Signs: `notices.failed` or SMTP/API errors in `worker-default`. Alarm e-mails may not arrive either, if they use the same provider.

- Temporary errors are retried. A permanent error marks the notice `failed`. After the outage, find the `failed` notices in the Outbox; their cases are planned again by the follow-up.
- If the provider suspended the account (for example over the bounce rate), turn the global `sending_enabled` off (the kill switch), sort it out with the provider and check the sending domain's reputation.

### Budget alarm

1. Grafana's Cost dashboard (or the System page) shows which model and purpose spent the money. Is it an evaluation run (`eval`) or real cases?
2. Real cases: look for a burst of candidates (Pipeline dashboard). A new keyword may match too widely: narrow the brand's keywords or whitelist the pattern.
3. Once the monthly budget is spent, new cases go to review without AI. Nothing stops. Raising the budget is a decision: change `AI_MONTHLY_BUDGET_USD` in `.env`, then run `docker compose up -d`.

### Host crash

Signs: no new cases, external uptime checks fail, no alarms at all (Alertmanager ran on the same host).

1. Get the host back (console, provider panel). Docker starts the stack on boot (`restart: unless-stopped`).
2. `docker compose ps`: every service should be healthy and `migrate` exited 0.
3. If the disk was full, see [DiskAlmostFull](#diskalmostfull).
4. Redis has no persistence: the queued jobs are gone, but the state is in Postgres. The reconciler and the outbox re-queue the work within minutes. After an hour, check the System page and **Failed jobs**.
5. If the host is lost, build a new one with [self-hosting.md](self-hosting.md#on-one-linux-server), copy `.env` back and [restore the backups](#restore-drill). You lose whatever happened after the last backup, the queued jobs and the Prometheus history.

## Restore drill

### Restoring a self-hosted server

The backups are a `pg_dump` custom-format file and a tar of the `objects` volume ([self-hosting.md](self-hosting.md#backups)). A restore replaces the whole database. You need the `.env` the backup was made with, or at least its `TOTP_ENCRYPTION_KEY`, and the same Kalkan release or an older one than the code you will run.

```sh
cd deploy/selfhost
docker compose down                                   # stops everything; volumes stay
docker volume rm kalkan-selfhost_postgres-data
docker compose up -d postgres                         # initdb recreates the roles from .env
docker compose exec -T postgres pg_restore -U postgres -d kalkan --exit-on-error < /var/backups/kalkan/kalkan-2026-10-01.dump
docker run --rm -v kalkan-selfhost_objects:/data -v /var/backups/kalkan:/backup alpine \
  sh -c 'rm -rf /data/* && tar xzf /backup/objects-2026-10-01.tar.gz -C /data'
docker compose up -d                                  # migrate brings the schema up to date
```

The dump contains no roles: the empty volume's init script creates `kalkan_owner`, `kalkan_api` and `kalkan_worker` with the passwords in the current `.env`, so a restore also works after you rotated them. Evidence files that are in the objects archive but not in the database are harmless.

### The drill

Prove once before going live, then every quarter, that a backup really restores, without touching the running stack. From the repository root, with the latest dump:

```sh
docker run -d --name kalkan-drill \
  -e POSTGRES_PASSWORD=drill -e DB_OWNER_PASSWORD=drill-owner \
  -e DB_API_PASSWORD=drill-api -e DB_WORKER_PASSWORD=drill-worker \
  -v "$PWD/deploy/selfhost/postgres/initdb:/docker-entrypoint-initdb.d:ro" \
  -v "$PWD/backend/sql:/opt/kalkan/sql:ro" postgres:16.15-trixie
until docker exec kalkan-drill pg_isready --host=127.0.0.1 -U kalkan_owner -d kalkan; do sleep 2; done
time docker exec -i kalkan-drill pg_restore -U postgres -d kalkan --exit-on-error < /var/backups/kalkan/kalkan-2026-10-01.dump
docker exec kalkan-drill psql -U postgres -d kalkan -c "
  SELECT (SELECT count(*) FROM customers) AS customers, (SELECT count(*) FROM cases) AS cases,
         (SELECT count(*) FROM notices) AS notices, (SELECT count(*) FROM artifacts) AS artifacts,
         (SELECT count(*) FROM audit_log) AS audit_rows, (SELECT max(created_at) FROM cases) AS newest_case,
         (SELECT version_num FROM alembic_version) AS migration"
docker rm -f kalkan-drill
```

The counts should be close to the live ones, the newest case should date from just before the backup, and the migration should match the running release. Also list a few files of the objects archive (`sudo tar tzf /var/backups/kalkan/objects-....tar.gz | head`). Write down how long the restore took: a real restore stops the pipeline for about that long.

### Reference deployment

Cloud SQL takes the backups there. `infra/scripts/restore-drill.sh` restores the latest one into a temporary instance, counts the same tables, prints the time each step took and deletes only the temporary instance:

```sh
PROJECT_ID=my-gcp-project infra/scripts/restore-drill.sh
```

## Rehearsal with a test tenant

Run the whole chain on the real server without anything reaching a real abuse desk: candidate → evidence → (AI) → review → dry-run preview in your mailbox. The follow-up and takedown steps are covered locally by `make e2e`.

**Preparation.** You need a clone page you control. Take `fixtures/sites/clone/index.html`, replace "Mintera" with your test brand name (for example "Kalkantest"), and upload it to a free static host under an address that contains the keyword, for example `kalkantest-login.<static-host>`. Remove it afterwards. The preview's nominal recipient is that hosting company, but nothing reaches it in dry-run.

1. Test tenant:

   ```sh
   docker compose exec ops kalkan customer create "Kalkan Test" --slug kalkan-test --alert-email you@example.com
   ```

   On its ops page turn on `is_test` (permanent dry-run) and `sending_enabled`, and enter an LOA date. Leave `monitor_only` off.

2. Brand, official domain and candidate:

   ```sh
   docker compose exec ops kalkan brand add Kalkantest --customer kalkan-test --keyword kalkantest --market PL --language en
   docker compose exec ops kalkan official-domain add kalkantest.example --brand <brand-id>
   docker compose exec ops kalkan candidate add https://kalkantest-login.<static-host>/ --brand <brand-id>
   ```

3. Follow the case in psql. Each step takes a few minutes at most:

   ```sql
   SELECT c.number, c.state, c.last_action FROM cases c JOIN customers cu ON cu.id = c.customer_id
   WHERE cu.slug = 'kalkan-test' ORDER BY c.created_at DESC;
   SELECT s.stage, s.profile, s.outcome, s.http_status FROM scans s JOIN cases c ON c.id = s.case_id
   WHERE c.number = '<number>' ORDER BY s.started_at;
   SELECT a.kind, a.profile, a.bytes FROM artifacts a JOIN cases c ON c.id = a.case_id WHERE c.number = '<number>';
   SELECT v.source, v.category, v.confidence, ai.model, ai.cost_usd FROM verdicts v
   LEFT JOIN ai_calls ai ON ai.id = v.ai_call_id JOIN cases c ON c.id = v.case_id WHERE c.number = '<number>';
   ```

   The case should reach `in_review`. With auto-approve off, the decision is yours.

4. In ops **Review**, open the case, look at the evidence and press `A P`.
5. Within minutes, e-mails prefixed "[DRY RUN]" reach `DRY_RUN_REDIRECT_EMAIL`, each naming the real recipient, channel and method. Every notice row should have `dry_run = true` and `blocked_reason = dry_run`, and none should be `sent`.
6. Remove the clone page. The case stays in follow-up and becomes `taken_down` 72 hours after the page disappears, which proves the follow-up on the real server too. Keep the test tenant for the next rehearsal.

If a step hangs, check the System page (queues, `job_failed`) and `docker compose logs --tail=200 worker-default worker-browser worker-ai`.

## Refreshing the demo workspace

A server can carry the Mintera Bank demo workspace for showing Kalkan to others (slug `mintera-demo`, flagged `is_demo`, read-only). Its dates are relative to the day it was seeded, so refresh it now and then:

```sh
docker compose exec ops kalkan seed --print-credentials
```

- It asks for confirmation (`--yes` skips the question). It writes only the `is_demo` customer `mintera-demo`. It stops without writing if a real customer uses that slug, or if a demo domain appears in a real customer's case.
- The two demo users get new passwords and TOTP secrets, shown once on the screen and stored nowhere. Their old sessions end. With `ENV=prod` this is the only way the seed runs: no credentials file is written.
- Use `exec`, not `run`: the output of a one-off `run` container goes to the Docker log.
- The demo's monthly report is CSV only; no print token is created.

On a laptop, `make demo` does all of this and also writes `.local/demo-credentials.json` ([self-hosting.md](self-hosting.md#on-a-laptop)).
