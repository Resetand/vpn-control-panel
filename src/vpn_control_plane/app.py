from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn

from vpn_control_plane.config import Settings
from vpn_control_plane.crons.base import App, run_registered_cron_jobs, wire_cron_jobs
from vpn_control_plane.data.store import ControlPlaneStore
from vpn_control_plane.http.routes import create_router
from vpn_control_plane.monitoring import run_monitoring_alerts
from vpn_control_plane.telegram.bot import run_telegram_bot

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, *, start_telegram: bool = True) -> App:
    settings = settings or Settings()  # type: ignore[call-arg]
    store = ControlPlaneStore(settings.data_file)
    store.verify_ready()
    store.load_state()
    logger.info("Application configuration and state loaded")

    @asynccontextmanager
    async def lifespan(_app: App) -> AsyncIterator[None]:
        bot_task = None
        monitoring_task = None
        if start_telegram:
            logger.info("Starting Telegram bot background task")
            bot_task = asyncio.create_task(run_telegram_bot(settings, store))
            bot_task.add_done_callback(
                lambda completed_task: _log_background_task_failure("Telegram bot", completed_task)
            )
        if settings.monitoring_alerts_enabled:
            logger.info("Starting monitoring alerts background task")
            monitoring_task = asyncio.create_task(run_monitoring_alerts(settings, store))
            monitoring_task.add_done_callback(
                lambda completed_task: _log_background_task_failure("Monitoring alerts", completed_task)
            )
        try:
            async with run_registered_cron_jobs(_app, settings, store):
                yield
        finally:
            if monitoring_task is not None:
                logger.info("Stopping monitoring alerts background task")
                monitoring_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await monitoring_task
            if bot_task is not None:
                logger.info("Stopping Telegram bot background task")
                bot_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await bot_task
            logger.info("Application shutdown complete")

    app = App(title="VPN Control Plane", lifespan=lifespan)
    app.state.settings = settings
    app.state.store = store
    wire_cron_jobs(app, settings)
    app.include_router(create_router(settings, store))
    return app


def _log_background_task_failure(name: str, task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    exception = task.exception()
    if exception is not None:
        logger.error(
            "%s background task stopped unexpectedly",
            name,
            exc_info=(type(exception), exception, exception.__traceback__),
        )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings()  # type: ignore[call-arg]
    uvicorn.run(
        "vpn_control_plane.app:create_app",
        factory=True,
        host=settings.http_host,
        port=settings.http_port,
    )
