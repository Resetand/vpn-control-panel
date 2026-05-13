from __future__ import annotations

import logging

from aiogram import Bot

from vpn_control_plane.config import Settings

logger = logging.getLogger(__name__)


class TelegramAlertNotifier:
    def __init__(self, settings: Settings, bot: Bot | None = None) -> None:
        self._settings = settings
        self._owns_bot = bot is None
        self._bot = bot or Bot(token=settings.telegram_bot_token.get_secret_value())

    async def send_alert(self, message: str) -> None:
        for admin_id_text in sorted(self._settings.admin_telegram_ids):
            try:
                admin_id = int(admin_id_text)
            except ValueError:
                logger.warning("Skipping monitoring alert for invalid admin id %r", admin_id_text)
                continue
            await self._bot.send_message(chat_id=admin_id, text=message)

    async def close(self) -> None:
        if self._owns_bot:
            await self._bot.session.close()
