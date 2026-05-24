# Runtime Stack

The runtime is a small Compose stack: one Python service hosts the HTTP subscription API and Telegram bot, and one bundled nginx service publishes the public TLS subscription endpoint.

## Choices

- Python 3.12 for the application runtime.
- FastAPI for HTTP routes, health checks, future subscription endpoints, and backup/export endpoints.
- aiogram 3 for Telegram polling or future webhook handling.
- httpx for 3x-UI API calls and node subscription fetching.
- Pydantic v2 and pydantic-settings for environment settings and JSON state validation.
- A mounted root-level `data.json` file for control-plane state.
- nginx 1.27 Alpine for TLS termination and public subscription reverse proxying.
- pytest and pytest-asyncio for behavior-focused unit tests.
- Ruff and mypy for linting, formatting, and static checks.

## Notes

Core orchestration lives in Python. nginx renders `nginx/templates/subscription.conf.esh` at container startup through the official nginx template entrypoint; a tiny shell helper only normalizes the route and checks that mounted TLS files exist.
