from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime

from aiogram import Bot
from aiogram.types import BufferedInputFile

from vpn_control_plane.backup import CONTROL_PLANE_BACKUP_FILE_NAME, SecretsBackupError, build_control_plane_backup
from vpn_control_plane.config import Settings
from vpn_control_plane.data import JsonStateStore
from vpn_control_plane.reports.schedule import next_cron_time

logger = logging.getLogger(__name__)
REPORT_CAPTION = "Автоматический report: бекап control-plane, секретов и 3x-UI нод."


async def send_telegram_backup_report(settings: Settings, store: JsonStateStore, bot: Bot) -> None:
    backup = await build_control_plane_backup(
        store.data_dir,
        store.load_nodes(),
        env_file=settings.backup_secrets_env_file,
        ssh_public_key=settings.backup_secrets_ssh_key,
    )
    for admin_id in sorted(settings.admin_telegram_ids):
        await bot.send_document(
            chat_id=int(admin_id),
            document=BufferedInputFile(backup, filename=CONTROL_PLANE_BACKUP_FILE_NAME),
            caption=REPORT_CAPTION,
        )


async def run_telegram_report_scheduler(
    settings: Settings,
    store: JsonStateStore,
    *,
    bot_factory: Callable[[Settings], Bot] | None = None,
) -> None:
    if not settings.report_telegram_enabled:
        logger.info("Telegram report scheduler is disabled")
        return

    bot = (bot_factory or _create_bot)(settings)
    try:
        while True:
            now = datetime.now(UTC)
            next_time = next_cron_time(settings.report_telegram_schedule, now)
            delay = max(0.0, (next_time - now).total_seconds())
            logger.info("Next Telegram report is scheduled at %s", next_time.isoformat())
            await asyncio.sleep(delay)
            try:
                await send_telegram_backup_report(settings, store, bot)
            except SecretsBackupError:
                logger.exception("Telegram report failed while encrypting secrets backup")
            except Exception:
                logger.exception("Telegram report failed")
    finally:
        await bot.session.close()


def _create_bot(settings: Settings) -> Bot:
    return Bot(token=settings.telegram_bot_token.get_secret_value())
