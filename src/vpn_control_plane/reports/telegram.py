from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import BufferedInputFile

from vpn_control_plane.backup import SecretsBackupError, build_full_backup
from vpn_control_plane.config import Settings
from vpn_control_plane.data import ControlPlaneStore
from vpn_control_plane.telegram import create_bot

logger = logging.getLogger(__name__)
REPORT_CAPTION = (
    "Автоматический report: бекап VPN (control plane, 3x-UI ноды, подключённые сервисы). "
    "Как восстановить — README.md внутри."
)


async def send_telegram_backup_report(settings: Settings, store: ControlPlaneStore, bot: Bot) -> None:
    backup = await build_full_backup(
        store.data_file,
        store.load_nodes(),
        ssh_public_key=settings.backup_secrets_ssh_key,
        env_file=settings.backup_secrets_env_file,
        external_subscriptions_file=settings.external_subscriptions_resolved_path,
        include_dir=settings.backup_include_dir,
    )
    for admin_id in sorted(settings.admin_telegram_ids):
        await bot.send_document(
            chat_id=int(admin_id),
            document=BufferedInputFile(backup.content, filename=backup.file_name),
            caption=REPORT_CAPTION,
        )


async def send_telegram_backup_report_to_admins(settings: Settings, store: ControlPlaneStore) -> None:
    bot = create_bot(settings)
    try:
        try:
            await send_telegram_backup_report(settings, store, bot)
        except SecretsBackupError:
            logger.exception("Telegram report failed: backup could not be built")
    finally:
        await bot.session.close()
