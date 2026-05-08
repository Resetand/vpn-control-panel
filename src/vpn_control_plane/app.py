from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from vpn_control_plane.config import Settings
from vpn_control_plane.data.store import JsonStateStore
from vpn_control_plane.http.routes import create_router
from vpn_control_plane.reports.telegram import run_telegram_report_scheduler
from vpn_control_plane.telegram.bot import run_telegram_bot

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, *, start_telegram: bool = True) -> FastAPI:
    settings = settings or Settings()  # type: ignore[call-arg]
    store = JsonStateStore(settings.data_dir)
    store.verify_ready()
    store.load_state()
    logger.info("Application configuration and state loaded")

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        bot_task: asyncio.Task[None] | None = None
        report_task: asyncio.Task[None] | None = None
        if start_telegram:
            logger.info("Starting Telegram background task")
            bot_task = asyncio.create_task(run_telegram_bot(settings, store))
            bot_task.add_done_callback(_log_bot_task_failure)
        if start_telegram and settings.report_telegram_enabled:
            logger.info("Starting Telegram report scheduler task")
            report_task = asyncio.create_task(run_telegram_report_scheduler(settings, store))
            report_task.add_done_callback(_log_bot_task_failure)
        try:
            yield
        finally:
            if report_task is not None:
                logger.info("Stopping Telegram report scheduler task")
                report_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await report_task
            if bot_task is not None:
                logger.info("Stopping Telegram background task")
                bot_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await bot_task
            logger.info("Application shutdown complete")

    app = FastAPI(title="VPN Control Plane", lifespan=lifespan)
    app.state.settings = settings
    app.state.store = store
    app.include_router(create_router(settings, store))
    return app


def _log_bot_task_failure(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    exception = task.exception()
    if exception is not None:
        logger.error("Telegram bot task stopped unexpectedly", exc_info=exception)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings()  # type: ignore[call-arg]
    uvicorn.run(
        "vpn_control_plane.app:create_app",
        factory=True,
        host=settings.http_host,
        port=settings.http_port,
    )
