.PHONY: init init-data restore-data backup-data backup-secrets up run down stop logs ps build test lint format typecheck clean

COMPOSE ?= docker compose
ENV_FILE ?= .env
BACKUP ?=
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
