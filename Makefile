.PHONY: init init-data restore-data backup-data backup-secrets migrate-subscription-ids up run down stop logs ps build test lint format typecheck clean

COMPOSE ?= docker compose
ENV_FILE ?= .env
BACKUP ?=
ENV_DATA_DIR := $(shell if test -f $(ENV_FILE); then sed -n 's/^VPN_HOST_DATA_DIR=//p' $(ENV_FILE) | tail -n 1; fi)
VPN_HOST_DATA_DIR ?= $(if $(ENV_DATA_DIR),$(ENV_DATA_DIR),./data)

init: $(ENV_FILE) init-data

$(ENV_FILE):
	cp .env.sample $(ENV_FILE)
	sed -i "s|^BACKUP_HTTP_TOKEN=.*|BACKUP_HTTP_TOKEN=\"$$(openssl rand -hex 8)\"|" "$(ENV_FILE)"

init-data:
	mkdir -p "$(VPN_HOST_DATA_DIR)"
	test -f "$(VPN_HOST_DATA_DIR)/nodes.json" || printf '[]\n' > "$(VPN_HOST_DATA_DIR)/nodes.json"
	test -f "$(VPN_HOST_DATA_DIR)/clients.json" || printf '[]\n' > "$(VPN_HOST_DATA_DIR)/clients.json"
	test -f "$(VPN_HOST_DATA_DIR)/inbounds.json" || printf '[]\n' > "$(VPN_HOST_DATA_DIR)/inbounds.json"
	test -f "$(VPN_HOST_DATA_DIR)/subscription.json" || printf '{}\n' > "$(VPN_HOST_DATA_DIR)/subscription.json"

restore-data:
	test -n "$(BACKUP)" || (echo "Usage: make restore-data BACKUP=/path/to/data.tar.gz" && exit 2)
	mkdir -p "$(VPN_HOST_DATA_DIR)"
	tar -xzf "$(BACKUP)" -C "$(VPN_HOST_DATA_DIR)"

backup-data: $(ENV_FILE) init-data
	mkdir -p backups
	$(COMPOSE) run --rm --build dev python -m vpn_control_plane.backup data --data-dir /app/data --output /app/backups/data-$$(date +%Y%m%d-%H%M%S).tar.gz

backup-secrets: $(ENV_FILE)
	mkdir -p backups
	$(COMPOSE) run --rm --build dev python -m vpn_control_plane.backup secrets --env-file /app/$(ENV_FILE) --output /app/backups/env.encrypted

migrate-subscription-ids: $(ENV_FILE) init-data
	$(COMPOSE) run --rm --build dev python -m vpn_control_plane.migrations.subscription_ids --data-dir /app/data

start: init
	VPN_ENV_FILE="$(ENV_FILE)" $(COMPOSE) --env-file "$(ENV_FILE)" up -d --build app nginx

build:
	VPN_ENV_FILE="$(ENV_FILE)" $(COMPOSE) --env-file "$(ENV_FILE)" build app nginx

stop:
	$(COMPOSE) down

restart:
	$(COMPOSE) down
	VPN_ENV_FILE="$(ENV_FILE)" $(COMPOSE) --env-file "$(ENV_FILE)" up -d --build app nginx

logs:
	VPN_ENV_FILE="$(ENV_FILE)" $(COMPOSE) --env-file "$(ENV_FILE)" logs -f -t app nginx

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
