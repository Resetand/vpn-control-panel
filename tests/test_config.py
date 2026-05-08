from __future__ import annotations

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
