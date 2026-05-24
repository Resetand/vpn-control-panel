from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from vpn_control_plane.config import Settings
from vpn_control_plane.data import ControlPlaneStore
from vpn_control_plane.reports.schedule import CronScheduleError, next_cron_time, validate_cron_expression
from vpn_control_plane.reports.telegram import REPORT_CAPTION, send_telegram_backup_report


def test_next_cron_time_supports_basic_crontab_syntax() -> None:
    now = datetime(2026, 5, 8, 6, 11, tzinfo=UTC)

    assert next_cron_time("*/15 6 * * *", now) == datetime(2026, 5, 8, 6, 15, tzinfo=UTC)
    assert next_cron_time("0 3 * * *", now) == datetime(2026, 5, 9, 3, 0, tzinfo=UTC)


def test_validate_cron_expression_rejects_invalid_syntax() -> None:
    with pytest.raises(CronScheduleError):
        validate_cron_expression("not a cron")


@pytest.mark.asyncio
async def test_send_telegram_backup_report_sends_archive_to_every_admin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings.model_validate(
        {
            "VPN_DATA_FILE": str(tmp_path / "data.json"),
            "VPN_TELEGRAM_BOT_TOKEN": "token",
            "VPN_TELEGRAM_ADMIN_IDS": "2,1",
        }
    )
    sent: list[dict[str, Any]] = []

    async def fake_build_backup(*_args: object, **_kwargs: object) -> bytes:
        return b"backup-archive"

    class FakeBot:
        async def send_document(self, **kwargs: Any) -> None:
            sent.append(kwargs)

    monkeypatch.setattr("vpn_control_plane.reports.telegram.build_control_plane_backup", fake_build_backup)
    (tmp_path / "data.json").write_text(
        json.dumps(
            {
                "nodes": [],
                "externalInbounds": [],
                "clients": [],
                "defaultClientInboundTags": [],
                "subscription": {},
            }
        ),
        encoding="utf-8",
    )

    await send_telegram_backup_report(settings, ControlPlaneStore(tmp_path / "data.json"), cast(Any, FakeBot()))

    assert [item["chat_id"] for item in sent] == [1, 2]
    assert [item["document"].filename for item in sent] == ["vpn-control-plane-backup.tar.gz"] * 2
    assert [item["caption"] for item in sent] == [REPORT_CAPTION] * 2
