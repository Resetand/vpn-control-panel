from __future__ import annotations

import tarfile
from io import BytesIO
from typing import Any, cast

import pytest

from vpn_control_plane.backup import build_node_backup_archive
from vpn_control_plane.data import NodeRecord


def node(node_id: int, host: str) -> NodeRecord:
    return NodeRecord.model_validate(
        {
            "id": node_id,
            "host": host,
            "port": 2053,
            "webBasePath": "panel",
            "username": "admin",
            "password": "password",
        }
    )


class FakeXuiNodeClient:
    def __init__(self, node_record: NodeRecord) -> None:
        self.node = node_record

    async def get_database_backup(self) -> bytes:
        if self.node.id == 2:
            raise RuntimeError("node is down")
        return f"db-{self.node.id}".encode()

    async def get_config_json_backup(self) -> bytes:
        return f"config-{self.node.id}".encode()

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_build_node_backup_archive_contains_database_and_config_per_available_node() -> None:
    backup = await build_node_backup_archive(
        [node(1, "eu.example.test"), node(2, "msk.example.test")],
        node_client_factory=cast(Any, FakeXuiNodeClient),
    )

    with tarfile.open(fileobj=BytesIO(backup), mode="r:gz") as archive:
        names = sorted(archive.getnames())
        assert names == [
            "eu.example.test-1-x-ui.db",
            "eu.example.test-1-xray-config.json",
            "node-backup-errors.txt",
        ]
        db_file = archive.extractfile("eu.example.test-1-x-ui.db")
        config_file = archive.extractfile("eu.example.test-1-xray-config.json")
        errors_file = archive.extractfile("node-backup-errors.txt")
        assert db_file is not None
        assert config_file is not None
        assert errors_file is not None
        assert db_file.read() == b"db-1"
        assert config_file.read() == b"config-1"
        assert "node 2 (msk.example.test): node is down" in errors_file.read().decode("utf-8")
