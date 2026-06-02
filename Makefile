.PHONY: init init-data restore-data backup-data backup-secrets sync up run down stop logs ps build test lint format typecheck clean \
        init-local run-local stop-local logs-local

COMPOSE ?= docker compose
ENV_FILE ?= .env
BACKUP ?=
DRY_RUN ?=
SYNC_FLAGS := $(if $(DRY_RUN),--dry-run,)
ENV_DATA_FILE := $(shell if test -f $(ENV_FILE); then sed -n 's/^VPN_HOST_DATA_FILE=//p' $(ENV_FILE) | tail -n 1; fi)
VPN_HOST_DATA_FILE ?= $(if $(ENV_DATA_FILE),$(ENV_DATA_FILE),./data.json)
VPN_HOST_DATA_DIR ?= $(patsubst %/,%,$(dir $(VPN_HOST_DATA_FILE)))
VPN_DATA_FILE_NAME ?= $(notdir $(VPN_HOST_DATA_FILE))
VPN_CONTAINER_DATA_FILE ?= /app/data/$(VPN_DATA_FILE_NAME)
COMPOSE_ENV := VPN_ENV_FILE="$(ENV_FILE)" VPN_HOST_DATA_DIR="$(VPN_HOST_DATA_DIR)" VPN_DATA_FILE_NAME="$(VPN_DATA_FILE_NAME)" VPN_CONTAINER_DATA_FILE="$(VPN_CONTAINER_DATA_FILE)"

init: $(ENV_FILE) init-data

$(ENV_FILE):
	cp .env.sample $(ENV_FILE)
	sed -i "s|^BACKUP_HTTP_TOKEN=.*|BACKUP_HTTP_TOKEN=\"$$(openssl rand -hex 8)\"|" "$(ENV_FILE)"

init-data:
	mkdir -p "$$(dirname "$(VPN_HOST_DATA_FILE)")"
	test -f "$(VPN_HOST_DATA_FILE)" || printf '{\n  "nodes": [],\n  "externalInbounds": [],\n  "clients": [],\n  "defaultClientInboundTags": [],\n  "subscription": {}\n}\n' > "$(VPN_HOST_DATA_FILE)"

restore-data:
	test -n "$(BACKUP)" || (echo "Usage: make restore-data BACKUP=/path/to/data.tar.gz" && exit 2)
	mkdir -p "$$(dirname "$(VPN_HOST_DATA_FILE)")"
	tar -xzf "$(BACKUP)" -O data.json > "$(VPN_HOST_DATA_FILE)"

backup-data: $(ENV_FILE) init-data
	mkdir -p backups
	$(COMPOSE_ENV) $(COMPOSE) run --rm --build dev python -m vpn_control_plane.backup data --data-file "$(VPN_CONTAINER_DATA_FILE)" --output /app/backups/data-$$(date +%Y%m%d-%H%M%S).tar.gz

backup-secrets: $(ENV_FILE)
	mkdir -p backups
	$(COMPOSE) run --rm --build dev python -m vpn_control_plane.backup secrets --env-file /app/$(ENV_FILE) --output /app/backups/env.encrypted

sync: $(ENV_FILE) init-data
	$(COMPOSE_ENV) $(COMPOSE) run --rm --build dev python -m vpn_control_plane.sync --data-file "$(VPN_CONTAINER_DATA_FILE)" $(SYNC_FLAGS)

start: init
	$(COMPOSE_ENV) $(COMPOSE) --env-file "$(ENV_FILE)" up -d --build app nginx

build:
	$(COMPOSE_ENV) $(COMPOSE) --env-file "$(ENV_FILE)" build app nginx

stop:
	$(COMPOSE) down

restart:
	$(COMPOSE) down
	$(COMPOSE_ENV) $(COMPOSE) --env-file "$(ENV_FILE)" up -d --build app nginx

logs:
	$(COMPOSE_ENV) $(COMPOSE) --env-file "$(ENV_FILE)" logs -f -t app nginx

test:
	$(COMPOSE) run --rm --build dev pytest

lint:
	$(COMPOSE) run --rm --build dev ruff check src tests

format:
	$(COMPOSE) run --rm --build dev ruff format src tests

typecheck:
	$(COMPOSE) run --rm --build dev mypy

clean:
	$(COMPOSE) down --remove-orphans
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage dist build *.egg-info

# ── Local dev stack ───────────────────────────────────────────────────────────
LOCAL_COMPOSE_FILE := local/docker-compose.local.yml
LOCAL_ENV_FILE := .env.local
LOCAL_DATA_FILE := ./data.local.json
INIT_ARGS ?=
XUI_VERSION ?=

# First-time setup: create config files from samples, then initialize node DBs.
# Use INIT_ARGS=--restore to populate node DBs from backup files instead of
# starting fresh (set XUI_<HOST_UPPER>_BACKUP_DB paths in .env.local first).
init-local: $(LOCAL_ENV_FILE) $(LOCAL_DATA_FILE)
	python3 local/init-nodes.py $(INIT_ARGS)

$(LOCAL_ENV_FILE):
	cp local/.env.local.sample $(LOCAL_ENV_FILE)
	@echo ""
	@echo "Created $(LOCAL_ENV_FILE) — fill in VPN_TELEGRAM_BOT_TOKEN and VPN_TELEGRAM_ADMIN_IDS before continuing."
	@echo ""

$(LOCAL_DATA_FILE):
	cp local/data.local.json.sample $(LOCAL_DATA_FILE)
	@echo ""
	@echo "Created $(LOCAL_DATA_FILE) — fill in basePath and apiToken for each node before continuing."
	@echo ""

# Start / restart the local stack (nodes + control plane).
# Patches node DBs with the current apiToken on every run (idempotent).
# Requires .env.local and data.local.json — run `make init-local` first.
# Pass XUI_VERSION=vX.Y.Z to pull a specific 3x-ui image and restart nodes.
run-local:
	@test -f $(LOCAL_ENV_FILE)  || (echo "$(LOCAL_ENV_FILE) not found — run: make init-local" && exit 1)
	@test -f $(LOCAL_DATA_FILE) || (echo "$(LOCAL_DATA_FILE) not found — run: make init-local" && exit 1)
	$(if $(XUI_VERSION),XUI_IMAGE_TAG=$(XUI_VERSION) )python3 local/init-nodes.py --patch-only
	$(if $(XUI_VERSION),$(COMPOSE) -f $(LOCAL_COMPOSE_FILE) --env-file $(LOCAL_ENV_FILE) pull)
	$(COMPOSE) -f $(LOCAL_COMPOSE_FILE) --env-file $(LOCAL_ENV_FILE) up -d --build $(if $(XUI_VERSION),--force-recreate)

# Stop the local stack.
stop-local:
	$(COMPOSE) -f $(LOCAL_COMPOSE_FILE) down

# Follow logs from all local services. Filter with: make logs-local SVC=app
logs-local:
	$(COMPOSE) -f $(LOCAL_COMPOSE_FILE) logs -f -t $(SVC)
