from __future__ import annotations

import pytest
from pydantic import ValidationError

from vpn_control_plane.config import Settings


def test_settings_strip_wrapping_quotes_from_id_sets_and_wildcard() -> None:
    settings = Settings.model_validate(
        {
            "VPN_TELEGRAM_BOT_TOKEN": "token",
            "VPN_TELEGRAM_ADMIN_IDS": '"123,456"',
            "VPN_TELEGRAM_ALLOWED_USER_IDS": '"*"',
        }
    )

    assert settings.admin_telegram_ids == {"123", "456"}
    assert settings.telegram_allowed_user_ids is None


def test_settings_strip_wrapping_quotes_from_backup_ssh_key() -> None:
    settings = Settings.model_validate(
        {
            "VPN_TELEGRAM_BOT_TOKEN": "token",
            "VPN_TELEGRAM_ADMIN_IDS": "1",
            "BACKUP_SECRETS_SSH_KEY": '"ssh-ed25519 AAAATEST backup"',
        }
    )

    assert settings.backup_secrets_ssh_key == "ssh-ed25519 AAAATEST backup"


def test_settings_parse_report_telegram_schedule() -> None:
    settings = Settings.model_validate(
        {
            "VPN_TELEGRAM_BOT_TOKEN": "token",
            "VPN_TELEGRAM_ADMIN_IDS": "1",
            "REPORT_TELEGRAM_ENABLED": "true",
            "REPORT_TELEGRAM_SCHEDULE": "*/30 * * * *",
        }
    )

    assert settings.report_telegram_enabled is True
    assert settings.report_telegram_schedule == "*/30 * * * *"


def test_settings_parse_geofiles_update_schedule() -> None:
    settings = Settings.model_validate(
        {
            "VPN_TELEGRAM_BOT_TOKEN": "token",
            "VPN_TELEGRAM_ADMIN_IDS": "1",
            "GEOFILES_UPDATE_ENABLED": "true",
            "GEOFILES_UPDATE_SCHEDULE": "15 4 * * 1",
        }
    )

    assert settings.geofiles_update_enabled is True
    assert settings.geofiles_update_schedule == "15 4 * * 1"


def test_settings_monitoring_defaults() -> None:
    settings = Settings.model_validate(
        {
            "VPN_TELEGRAM_BOT_TOKEN": "token",
            "VPN_TELEGRAM_ADMIN_IDS": "1",
        }
    )

    assert settings.monitoring_alerts_enabled is False
    assert settings.monitoring_poll_interval_seconds == 30
    assert settings.monitoring_failure_duration_seconds == 60
    assert settings.monitoring_cpu_threshold_percent == 90.0
    assert settings.monitoring_ram_threshold_percent == 90.0
    assert settings.monitoring_alert_cooldown_seconds == 3600


def test_settings_monitoring_custom_values() -> None:
    settings = Settings.model_validate(
        {
            "VPN_TELEGRAM_BOT_TOKEN": "token",
            "VPN_TELEGRAM_ADMIN_IDS": "1",
            "VPN_MONITORING_ALERTS_ENABLED": "true",
            "VPN_MONITORING_POLL_INTERVAL_SECONDS": "15",
            "VPN_MONITORING_FAILURE_DURATION_SECONDS": "120",
            "VPN_MONITORING_CPU_THRESHOLD_PERCENT": "80.5",
            "VPN_MONITORING_RAM_THRESHOLD_PERCENT": "75.5",
            "VPN_MONITORING_ALERT_COOLDOWN_SECONDS": "600",
        }
    )

    assert settings.monitoring_alerts_enabled is True
    assert settings.monitoring_poll_interval_seconds == 15
    assert settings.monitoring_failure_duration_seconds == 120
    assert settings.monitoring_cpu_threshold_percent == 80.5
    assert settings.monitoring_ram_threshold_percent == 75.5
    assert settings.monitoring_alert_cooldown_seconds == 600


def test_settings_monitoring_rejects_invalid_values() -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate(
            {
                "VPN_TELEGRAM_BOT_TOKEN": "token",
                "VPN_TELEGRAM_ADMIN_IDS": "1",
                "VPN_MONITORING_POLL_INTERVAL_SECONDS": "0",
            }
        )

    with pytest.raises(ValidationError):
        Settings.model_validate(
            {
                "VPN_TELEGRAM_BOT_TOKEN": "token",
                "VPN_TELEGRAM_ADMIN_IDS": "1",
                "VPN_MONITORING_CPU_THRESHOLD_PERCENT": "101",
            }
        )
