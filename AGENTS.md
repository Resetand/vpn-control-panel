This file provides guidance to Coding Agents working in this repository.
The project author prefers to keep russian language for a communication with agents.

## What this is

A self-hosted VPN control plane. Users interact through a **Telegram bot** to get VPN access; their VPN clients receive connection links via an **HTTP subscription endpoint**. The system wraps one or more [3x-ui](https://github.com/MHSanaei/3x-ui) panels and keeps them consistent with a single JSON file (`data.json`) as the source of truth.

## Project layout

```
src/vpn_control_plane/   — application source
  app.py                 — FastAPI factory + lifespan (composition root)
  config.py              — settings (env vars)
  provisioning.py        — client provisioning across nodes
  data/                  — state models and store (reads/writes data.json)
  subscription/          — subscription link assembly and HTTP rendering
  xui/                   — 3x-ui panel API client
  telegram/              — Telegram bot (aiogram)
  http/                  — FastAPI routes
  monitoring/            — node health polling and alerting
  crons/                 — scheduled jobs (geofiles, reports)
  backup/                — backup assembly
tests/                   — pytest test suite
data.json                — runtime state (topology + clients);
.env                     — environment config; copy from .env.sample
```

## Running the project

**Production** — Docker Compose is the primary deployment mechanism:

```
make init      # first-time setup: creates .env from .env.sample and empty data.json
make start     # build images and start (docker compose up -d)
make stop      # stop containers
make restart   # rebuild and restart
make logs      # follow logs
make sync      # reconcile data.json state → 3x-ui panels (run after editing data.json)
```

**Development** — tests and linters also run inside Docker (no local Python install required):

```
make test        # pytest
make lint        # ruff check
make format      # ruff format
make typecheck   # mypy
```

**Local stack** — real 3x-ui nodes + control plane in Docker, driven by `data.local.json`:

```
make init-local                      # first time: init node DBs (bootstrap or seed from backup.db)
make init-local INIT_ARGS=--restore  # re-seed from local/nodes/<host>/db/backup.db
make run-local                       # start / restart everything
make run-local XUI_VERSION=vX.X.X    # start nodes with specified version (equvalent to production update if db exists)
make stop-local                      # stop everything
make logs-local [SVC=app]            # follow logs
```

- `data.local.json` and `.env.local` are the config sources;
- `Node DBs/certs` live in `local/nodes/<host>/{db,certs}/`
- `local/docker-compose.local.yml` is generated automatically

## 3x-ui API

`openapi.3x-ui-3.2.0.json` — OpenAPI spec for the 3x-ui panel API (version 3.2.0). Use it as the reference when reading or modifying `src/vpn_control_plane/xui/`.

## Core domain concepts

**Nodes and inbounds.** A *node* is a 3x-ui panel instance. Each node has one or more *inbounds* — VPN entry points (e.g., VLESS over TCP). Every inbound — whether on a node or a static external URI — has a unique short *tag*. Tags are the routing unit for both provisioning and subscription delivery.

**Clients.** A *client* is a provisioned VPN user. Clients receive a set of inbound tags (either the global default list or a per-client override). A client's subscription URL resolves to a bundle of connection links — one per allowed inbound.

**External inbounds.** Static URIs (e.g., WireGuard configs) can be declared alongside node inbounds. Same tag-based routing, served as static links.

## Key invariants

**Provisioning is idempotent.** Creating a client that already exists on a node is safe — the code checks before adding and only fills in missing inbounds. Concurrent requests for the same client are serialized per-client.

**Subscription delivery is fault-tolerant.** If one node is unreachable, links from other nodes are still returned. Traffic stats are best-effort and never block link delivery.

**Subscription IDs have a migration path.** Clients have a live `subId` and an optional legacy ID; old URLs redirect to the canonical one rather than returning 404.

**Monitoring uses a cooldown state machine.** An alert fires only after a condition persists past a configured threshold and won't repeat until a cooldown expires. State is in-memory and resets on restart.
