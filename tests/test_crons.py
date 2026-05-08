from __future__ import annotations

from vpn_control_plane.config import Settings
from vpn_control_plane.crons.base import App, wire_cron_jobs


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "VPN_TELEGRAM_BOT_TOKEN": "token",
        "VPN_TELEGRAM_ADMIN_IDS": "1",
    }
    values.update(overrides)
    return Settings.model_validate(values)


def test_wire_cron_jobs_registers_enabled_jobs() -> None:
    app = App()

    wire_cron_jobs(
        app,
        settings(
            REPORT_TELEGRAM_ENABLED="true",
            REPORT_TELEGRAM_SCHEDULE="5 3 * * *",
            GEOFILES_UPDATE_ENABLED="true",
            GEOFILES_UPDATE_SCHEDULE="10 4 * * *",
        ),
    )

    assert [(job.name, job.schedule) for job in app.cron_jobs] == [
        ("Telegram report", "5 3 * * *"),
        ("geofiles update", "10 4 * * *"),
    ]


def test_wire_cron_jobs_skips_disabled_jobs() -> None:
    app = App()

    wire_cron_jobs(app, settings())

    assert app.cron_jobs == []
