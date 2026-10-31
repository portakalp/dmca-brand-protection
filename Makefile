# Kalkan developer commands. Run `make help` for the list.
SHELL := /bin/bash
.DEFAULT_GOAL := help

# COMPOSE_OVERRIDE: an optional extra compose file layered on top, e.g. other subnets when
# 172.30.0.0/16 is taken on your machine. Host ports move with variables (API_PORT=..., see
# infra/compose/docker-compose.yml), set in the shell or in .env.
COMPOSE_OVERRIDE ?=
COMPOSE := docker compose --env-file .env -f infra/compose/docker-compose.yml \
	$(if $(COMPOSE_OVERRIDE),-f $(COMPOSE_OVERRIDE))
ALL_PROFILES := --profile demo --profile ct --profile browser --profile ai --profile monitoring
INFRA_SERVICES := postgres redis fake-gcs mailpit fixture-sites dev-proxy
BACKEND := cd backend &&

# Tool images for infra-check (no local installs, no cloud credentials; ci.yml pins the same
# Terraform and TFLint versions).
TERRAFORM_IMAGE := hashicorp/terraform:1.16.5
TFLINT_IMAGE := ghcr.io/terraform-linters/tflint:v0.64.0
SHELLCHECK_IMAGE := koalaman/shellcheck:v0.11.0
PROMETHEUS_IMAGE := prom/prometheus:v3.13.4
ALERTMANAGER_IMAGE := prom/alertmanager:v0.34.1
CADDY_IMAGE := caddy:2.10.2-alpine
TF_DIR := infra/terraform
VM_COMPOSE := docker compose -f infra/compose/docker-compose.vm.yml
SELFHOST_COMPOSE := docker compose -f deploy/selfhost/docker-compose.yml --env-file deploy/selfhost/.env.example
EDGE_DIR := infra/cloudflare
WEB_DIR := web
# The API the panel talks to locally (next.config.ts rewrites /api/* there).
API_PORT ?= 8000
API_ORIGIN ?= http://localhost:$(API_PORT)

# The demo (`make demo`): only Docker and make on the host. demo-setup writes .local/ on the
# host, so it runs as the host user.
DEMO_COMPOSE := $(COMPOSE) --profile demo
DEMO_SERVICES := postgres redis fake-gcs mailpit api worker-default scheduler ops web
export HOST_UID ?= $(shell id -u)
export HOST_GID ?= $(shell id -g)

.PHONY: help up down logs ps shell up-infra up-ct up-browser up-ai up-monitoring alert-test load test test-unit test-integration e2e bench lint fmt \
	migrate seed openapi infra-check edge-test eval web-types web-dev web-lint web-test web-e2e \
	demo demo-browser demo-info demo-urls demo-code demo-down demo-reset

help: ## List the available targets
	@grep -E '^[a-zA-Z0-9_-]+:.*## ' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*## "}; {printf "  %-18s %s\n", $$1, $$2}'

.env:
	@echo ".env is missing. Create it with: cp .env.example .env" >&2; exit 1

up: .env ## Build and start the local stack and wait until every service is healthy
	$(COMPOSE) up -d --build --wait

down: .env ## Stop the local stack, demo, ct, browser, ai and monitoring profiles included (volumes are kept)
	$(COMPOSE) $(ALL_PROFILES) down

logs: .env ## Follow the logs of all services
	$(COMPOSE) logs -f --tail=200

ps: .env ## Show service status
	$(COMPOSE) ps

shell: .env ## Open a shell in the api container
	$(COMPOSE) exec api bash

up-infra: .env ## Start only the infrastructure services the tests use
	$(COMPOSE) up -d --wait $(INFRA_SERVICES)

up-ct: .env ## Also start certstream and ct-consumer (connects to the real CT logs)
	$(COMPOSE) --profile ct up -d --build --wait

up-browser: .env ## Also build and start worker-browser (Playwright image; pages go through dev-proxy)
	$(COMPOSE) --profile browser up -d --build --wait

up-ai: .env ## Also start worker-ai (real Claude calls: needs ANTHROPIC_API_KEY, costs money)
	$(COMPOSE) --profile ai up -d --build --wait

up-monitoring: .env ## Also start Prometheus and Alertmanager with the VM's alert rules (mail to Mailpit)
	$(COMPOSE) --profile monitoring up -d --build --wait

# --- Demo -------------------------------------------------------------------------------------
# AI stays off in the demo (worker-ai is not started), even with ANTHROPIC_API_KEY in the shell,
# unless AI_ENABLED is set explicitly.
# Creates .env from .env.example when it is missing (never overwrites it), builds the images,
# starts the stack with the production panel (migrate runs first), seeds Mintera Bank, renews the
# ops operator and prints the logins. Re-running re-seeds with new passwords.
# Without make: cp .env.example .env; mkdir -p .local; then
#   docker compose --env-file .env -f infra/compose/docker-compose.yml --profile demo up -d --build --wait web
#   docker compose --env-file .env -f infra/compose/docker-compose.yml --profile demo run --rm demo-setup
define demo_up
	@test -f .env || { cp .env.example .env && echo "Created .env from .env.example"; }
	@mkdir -p .local && chmod 700 .local
	AI_ENABLED=$${AI_ENABLED:-false} $(DEMO_COMPOSE) $(1) up -d --build --wait $(2)
	$(DEMO_COMPOSE) run --rm demo-setup
	@$(MAKE) --no-print-directory demo-urls
endef

demo: ## One-command demo: build, start, seed Mintera Bank, print URLs and logins (needs only Docker)
	$(call demo_up,,$(DEMO_SERVICES))

demo-browser: ## The demo plus worker-browser, which captures the fixture sites through dev-proxy
	$(call demo_up,--profile browser,$(DEMO_SERVICES) fixture-sites dev-proxy worker-browser)

demo-info: ## Show the demo URLs and logins again (from .local/demo-credentials.json)
	@$(DEMO_COMPOSE) run --rm --no-deps demo-setup kalkan demo show
	@$(MAKE) --no-print-directory demo-urls

demo-urls:
	@port() { $(DEMO_COMPOSE) port "$$1" "$$2" 2>/dev/null | sed 's/^127\.0\.0\.1:/http:\/\/localhost:/'; }; \
	web=$$(port web 3000); api=$$(port api 8000); ops=$$(port ops 8081); mail=$$(port mailpit 8025); \
	echo; \
	echo "Kalkan demo"; \
	echo "  Panel      $${web:-not running}  (security@mintera.example: owner, analyst@mintera.example: viewer)"; \
	echo "  Ops panel  $${ops:-not running}  (operator@kalkan.local)"; \
	echo "  Mailpit    $${mail:-not running}  (every e-mail the stack sends)"; \
	echo "  API docs   $${api:-not running}/api/docs"; \
	echo "Passwords and otpauth:// URIs (scan them into an authenticator) are listed above and in"; \
	echo ".local/demo-credentials.json. A sign-in asks for a TOTP code; without an app:"; \
	echo "  make demo-code EMAIL=security@mintera.example"; \
	echo "More: make demo-browser (captures), make demo-info, make demo-down, make demo-reset"

demo-code: ## Current TOTP code of a demo login: make demo-code EMAIL=security@mintera.example
	@test -n "$(EMAIL)" || { echo "Usage: make demo-code EMAIL=security@mintera.example" >&2; exit 2; }
	@$(DEMO_COMPOSE) --progress quiet run --rm --no-deps demo-setup kalkan demo code --one-line "$(EMAIL)"

demo-down: down ## Stop the demo (and every other profile); data is kept

demo-reset: ## Stop the stack and delete its volumes (database, stored objects); asks first
	@project=$$($(COMPOSE) config 2>/dev/null | sed -n 's/^name: //p' | head -n 1); \
	echo "This stops compose project '$$project' and deletes its volumes: the database (the tests'"; \
	echo "kalkan_test included), the stored objects and the browser hand-off. .env and .local/ stay."; \
	read -r -p "Type the project name to confirm: " answer; \
	test -n "$$project" && test "$$answer" = "$$project" || { echo "Cancelled."; exit 1; }
	$(COMPOSE) $(ALL_PROFILES) down -v --remove-orphans

alert-test: up-monitoring ## Fire one alarm on the local stack and find its e-mail in Mailpit
	$(BACKEND) uv run pytest -m alerting

load: up-infra ## Local load check: a CT burst with fake AI and proxy clients (docs/load-test.md)
	$(BACKEND) uv run python -m tests.load.ct_burst $(LOAD_ARGS)

test: up-infra ## Backend unit and integration tests
	$(BACKEND) uv run pytest -m "unit or integration"

test-unit: ## Fast backend tests without external services
	$(BACKEND) uv run pytest -m unit

test-integration: up-infra ## Backend tests against the compose services
	$(BACKEND) uv run pytest -m integration

e2e: up-infra ## The pipeline end to end: fixture clone, Chromium, Mailpit, follow-up (§25)
	$(BACKEND) uv run pytest -m e2e

bench: ## Throughput checks (matcher), not part of make test
	$(BACKEND) uv run pytest -m benchmark -s

lint: ## ruff, ruff format --check and mypy
	$(BACKEND) uv run ruff check . && uv run ruff format --check . && uv run mypy

fmt: ## Format the backend and apply safe ruff fixes
	$(BACKEND) uv run ruff format . && uv run ruff check --fix .

migrate: up-infra ## Alembic upgrade head on the development database (kalkan_owner)
	$(BACKEND) uv run alembic upgrade head

seed: migrate ## Demo data (Mintera Bank); credentials go to .local/demo-credentials.json
	$(BACKEND) uv run kalkan seed

# The newest exported dataset unless DATASET= is given; MODELS= is a space-separated list.
DATASET ?= $(lastword $(sort $(wildcard $(CURDIR)/eval/datasets/*.jsonl)))
MODELS ?= claude-haiku-5-5
LIMIT ?= 5
eval: ## AI evaluation of DATASET with MODELS, first LIMIT cases (real API calls; costs money)
	@test -n "$(DATASET)" || { echo "No dataset: run 'cd backend && uv run kalkan eval export' first" >&2; exit 1; }
	$(BACKEND) uv run kalkan eval run --dataset "$(DATASET)" $(foreach m,$(MODELS),--model $(m)) --limit $(LIMIT)

openapi: ## Write the API schema to backend/openapi.json
	$(BACKEND) uv run python -m kalkan.api.openapi

# The repo is mounted read-only (Terraform reads infra/vm/startup.sh); .terraform stays inside
# the container, the committed lock file is never rewritten (-lockfile=readonly), and provider
# and plugin downloads are cached in Docker volumes.
infra-check: ## Terraform fmt/validate, tflint, VM and self-host compose config, Caddyfile, alert rules, the VM Alertmanager template and shellcheck
	docker run --rm -v "$(CURDIR)":/repo:ro -w /repo/$(TF_DIR) -e TF_IN_AUTOMATION=1 \
		-e TF_DATA_DIR=/tmp/terraform -e TF_PLUGIN_CACHE_DIR=/plugin-cache \
		-v kalkan-terraform-plugins:/plugin-cache --entrypoint sh $(TERRAFORM_IMAGE) -c \
		'terraform fmt -check -recursive -diff && \
		terraform init -backend=false -input=false -lockfile=readonly >/dev/null && \
		terraform validate'
	docker run --rm -v "$(CURDIR)":/repo:ro -w /repo/$(TF_DIR) -e TFLINT_PLUGIN_DIR=/plugins \
		-e GITHUB_TOKEN -v kalkan-tflint-plugins:/plugins --entrypoint sh $(TFLINT_IMAGE) -c \
		'tflint --init >/dev/null && tflint --format compact'
	$(VM_COMPOSE) $$($(VM_COMPOSE) config --profiles | sed 's/^/--profile /') config --quiet
	$(SELFHOST_COMPOSE) config --quiet
	$(SELFHOST_COMPOSE) $$($(SELFHOST_COMPOSE) config --profiles | sed 's/^/--profile /') config --quiet
	docker run --rm -v "$(CURDIR)/deploy/selfhost/Caddyfile":/etc/caddy/Caddyfile:ro \
		-e APP_DOMAIN=app.example.com -e EVIDENCE_DOMAIN=evidence.example.com \
		-e ACME_EMAIL=admin@example.com -e EDGE_SHARED_SECRET=check $(CADDY_IMAGE) \
		caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
	docker run --rm -v "$(CURDIR)/infra/compose":/c:ro -w /c/vm/prometheus/tests \
		--entrypoint promtool $(PROMETHEUS_IMAGE) test rules kalkan_test.yml
	docker run --rm -v "$(CURDIR)/infra/compose":/c:ro -v "$(CURDIR)/deploy/selfhost":/s:ro \
		--entrypoint sh $(PROMETHEUS_IMAGE) -c \
		'promtool check rules /c/vm/prometheus/rules/*.yml && \
		promtool check config --syntax-only /c/vm/prometheus/prometheus.yml && \
		promtool check config --syntax-only /c/monitoring/prometheus.yml && \
		promtool check config --syntax-only /s/monitoring/prometheus.yml'
	docker run --rm -v "$(CURDIR)/infra/compose":/c:ro --entrypoint amtool $(ALERTMANAGER_IMAGE) \
		check-config /c/monitoring/alertmanager.yml
	for user in alerts@example.com ""; do \
		docker run --rm -v "$(CURDIR)/infra/compose/vm":/vm:ro --entrypoint sh \
			-e ALERT_SMTP_SMARTHOST=smtp.example.com:587 -e ALERT_SMTP_USERNAME="$$user" \
			-e ALERT_SMTP_PASSWORD=sample -e ALERT_MAIL_FROM=alerts@example.com \
			-e OPS_ALERT_EMAIL=ops@example.com $(ALERTMANAGER_IMAGE) -c \
			'out=$$(mktemp -d) && \
			sh /vm/bin/render-alertmanager.sh /vm/alertmanager/alertmanager.yml.tmpl "$$out" && \
			grep -q "^  smtp_smarthost: .smtp.example.com:587.$$" "$$out/alertmanager.yml" && \
			amtool check-config "$$out/alertmanager.yml"' || exit 1; \
	done
	find infra deploy -type f -name '*.sh' -not -path '*/node_modules/*' -not -path '*/.terraform/*' \
		-print0 | xargs -0 docker run --rm -v "$(CURDIR)":/mnt:ro -w /mnt $(SHELLCHECK_IMAGE) \
		--external-sources --source-path=SCRIPTDIR
	@echo "infra-check: ok"

$(EDGE_DIR)/node_modules/.package-lock.json: $(EDGE_DIR)/package-lock.json
	cd $(EDGE_DIR) && npm ci

edge-test: $(EDGE_DIR)/node_modules/.package-lock.json ## Edge Worker typecheck and tests (npm ci when the lock file changed)
	cd $(EDGE_DIR) && npm run typecheck && npm test

$(WEB_DIR)/node_modules/.package-lock.json: $(WEB_DIR)/package-lock.json
	cd $(WEB_DIR) && npm ci

web-types: $(WEB_DIR)/node_modules/.package-lock.json ## Web API types from backend/openapi.json (run make openapi first if the API changed)
	cd $(WEB_DIR) && npm run types

web-dev: $(WEB_DIR)/node_modules/.package-lock.json ## The panel on http://localhost:3000 against API_ORIGIN (default: make up's api)
	cd $(WEB_DIR) && API_ORIGIN=$(API_ORIGIN) npm run dev

web-lint: $(WEB_DIR)/node_modules/.package-lock.json ## eslint, prettier --check and tsc for the panel
	cd $(WEB_DIR) && npm run lint

web-test: $(WEB_DIR)/node_modules/.package-lock.json ## The panel's unit tests (Vitest)
	cd $(WEB_DIR) && npm test

# Needs `make up` (the api); the demo is re-seeded so its dates match today. Chromium once:
# cd web && npx playwright install chromium
web-e2e: $(WEB_DIR)/node_modules/.package-lock.json seed ## Panel end to end with Playwright and axe; screenshots in web/test-results/
	cd $(WEB_DIR) && API_ORIGIN=$(API_ORIGIN) npx playwright test
