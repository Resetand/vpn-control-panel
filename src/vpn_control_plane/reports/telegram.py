from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import BufferedInputFile

from vpn_control_plane.backup import CONTROL_PLANE_BACKUP_FILE_NAME, SecretsBackupError, build_control_plane_backup
from vpn_control_plane.config import Settings
from vpn_control_plane.data import ControlPlaneStore

logger = logging.getLogger(__name__)
REPORT_CAPTION = "Автоматический report: бекап control-plane, секретов и 3x-UI нод."


async def send_telegram_backup_report(settings: Settings, store: ControlPlaneStore, bot: Bot) -> None:
    backup = await build_control_plane_backup(
        store.data_file,
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


async def send_telegram_backup_report_to_admins(settings: Settings, store: ControlPlaneStore) -> None:
    bot = _create_bot(settings)
    try:
        try:
            await send_telegram_backup_report(settings, store, bot)
        except SecretsBackupError:
            logger.exception("Telegram report failed while encrypting secrets backup")
    finally:
        await bot.session.close()


def _create_bot(settings: Settings) -> Bot:
    return Bot(token=settings.telegram_bot_token.get_secret_value())
