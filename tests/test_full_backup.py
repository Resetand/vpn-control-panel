from __future__ import annotations

import json
import tarfile
from io import BytesIO
from pathlib import Path
from typing import Any, cast

import pytest

import vpn_control_plane.backup.secrets as backup_secrets
from vpn_control_plane.backup import build_control_plane_backup
from vpn_control_plane.data import NodeRecord


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def node(node_id: int, host: str) -> NodeRecord:
    return NodeRecord.model_validate(
        {
            "id": node_id,
            "host": host,
            "port": 2053,
            "basePath": "panel",
            "apiToken": "token",
        }
    )


class FakeXuiNodeClient:
    def __init__(self, node_record: NodeRecord) -> None:
        self.node = node_record

    async def get_database_backup(self) -> bytes:
        return f"db-{self.node.id}".encode()

    async def get_config_json_backup(self) -> bytes:
        return f"config-{self.node.id}".encode()

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_build_control_plane_backup_contains_data_encrypted_env_and_node_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_json(tmp_path / "nodes.json", [{"id": 1}])
    write_json(tmp_path / "clients.json", [])
    write_json(tmp_path / "inbounds.json", [])
    write_json(tmp_path / "subscription.json", {})
    write_json(tmp_path / "runtime-cache.json", {"ignored": True})
    env_file = tmp_path / ".env"
    env_file.write_text("VPN_TELEGRAM_BOT_TOKEN=secret\n", encoding="utf-8")
    monkeypatch.setattr(backup_secrets, "encrypt_for_ssh_public_key", lambda _plaintext, _key: b"encrypted-env")

    backup = await build_control_plane_backup(
        tmp_path,
        [node(1, "eu.example.test")],
        env_file=env_file,
        ssh_public_key="ssh-ed25519 AAAATEST backup",
        node_client_factory=cast(Any, FakeXuiNodeClient),
    )

    with tarfile.open(fileobj=BytesIO(backup), mode="r:gz") as archive:
        names = sorted(archive.getnames())
        assert names == [
            "data/clients.json",
            "data/inbounds.json",
            "data/nodes.json",
            "data/subscription.json",
            "env.encrypted",
            "eu.example.test-1-x-ui.db",
            "eu.example.test-1-xray-config.json",
        ]
        encrypted_env = archive.extractfile("env.encrypted")
        database = archive.extractfile("eu.example.test-1-x-ui.db")
        config = archive.extractfile("eu.example.test-1-xray-config.json")
        assert encrypted_env is not None
        assert database is not None
        assert config is not None
        assert encrypted_env.read() == b"encrypted-env"
        assert database.read() == b"db-1"
        assert config.read() == b"config-1"
