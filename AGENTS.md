This file provides guidance to Coding Agents when working with code in this repository.

## Project

A self-hosted control plane that turns a Telegram bot into the front door for a VPN service. It wraps one or more [3x-ui](https://github.com/MHSanaei/3x-ui) panels, manages subscriptions, issues subscription links, and automates trials, payments, and expiration.

## Commands

Requires Python 3.11+ and [uv](https://github.com/astral-sh/uv). Common tasks are in the `Makefile`:

- `make install` — `uv sync` (install/lock dependencies)
- `make run` — run the bot (`uv run vpn-control-plane`, entry point `vpn_control_plane.app:main`)
- `make test` — `uv run pytest`
- `make lint` — `uv run ruff check src tests`
- `make fmt` — `uv run ruff format src tests`
- `make migrate` — `uv run alembic upgrade head`

Run a single test: `uv run pytest tests/test_subscription_service.py::test_start_trial_provisions_clients`

Tests live in `tests/`; `pytest` is configured (in `pyproject.toml`) with `pythonpath = ["src"]`, so imports use the installed package path `vpn_control_plane.*`. Async tests are written with `@pytest.mark.asyncio`.

Ruff: line length 100, target py311, rule sets `E, F, I, UP, B, ASYNC` (see `ruff.toml`).

## Configuration

Runtime config is read from `config.toml` (copy `config.example.toml`). `config.py` parses it with `tomllib` into plain `@dataclass` objects (`AppConfig` → `TelegramConfig`, `PanelConfig[]`, `SubscriptionConfig`, `StateConfig`). Panels are a TOML array of tables (`[[panels]]`), so multiple 3x-ui panels are supported and the service fans out across all of them.

## Architecture

The control plane keeps its own source-of-truth state (`data.json`) and reconciles it with the 3x-ui panels so the two stay consistent. Layers under `src/vpn_control_plane/`:

- **`app.py`** — composition root. `_run()` loads config, builds the `StateStore`, one `XUIClient` per panel, the `SubscriptionService`, and the `Notifier`, then starts the notifier as a background task and runs aiogram polling.
- **`bot/`** — aiogram dispatcher. `build_dispatcher()` wires up `handlers.py` (commands, trial, menu) and `payments.py` (Telegram Stars invoices, pre-checkout, `successful_payment` → `service.renew`). `keyboards.py` holds inline keyboards.
- **`subscription/service.py`** — lifecycle logic: `start_trial`, `renew`, `expire_due`. It owns provisioning: `_provision_clients` calls `add_client` on every panel; `_deprovision_clients` removes a user's clients on expiry. Every mutation calls `store.save()`.
- **`data/`** — `state_store.py` is the durable JSON store: an `asyncio.Lock` guards writes, and `_write_atomic` writes to a tempfile then `os.replace`s it into place (atomic, crash-safe). `models.py` defines the dataclasses (`State`, `User`, `Client`, `SubscriptionStatus`) plus their `to_dict`/`from_dict` JSON (de)serialization — user-id keys are stored as strings in JSON and coerced back to `int`.
- **`xui/`** — `client.py` wraps a 3x-ui panel's REST API over `httpx.AsyncClient` (lazy cookie login, `add_client`/`remove_client`). Note 3x-ui expects the client list as a JSON-encoded *string* in the `settings` field. `share_links.py` builds a user's subscription link as `{sub_link_base}/{base64url(user_id)}`.
- **`notifications/notifier.py`** — background loop that calls `service.expire_due()` every hour.


## Docker

`docker-compose.yml` builds from the `Dockerfile` and bind-mounts `config.toml` (read-only) and `data.json` into the container.
