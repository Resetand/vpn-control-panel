from __future__ import annotations

import logging
from typing import Any

from vpn_control_plane.config import Settings
from vpn_control_plane.data import ControlPlaneStore
from vpn_control_plane.reports.telegram import send_telegram_backup_report_to_admins

logger = logging.getLogger(__name__)


async def send_telegram_report(settings: Settings, store: ControlPlaneStore) -> None:
    await send_telegram_backup_report_to_admins(settings, store)


def register(app: Any, settings: Settings) -> None:
    if not settings.report_telegram_enabled:
        logger.info("Telegram report cron job is disabled")
        return
    app.add_cron_job(send_telegram_report, settings.report_telegram_schedule, name="Telegram report")
