from __future__ import annotations

import tarfile
from collections.abc import Callable, Sequence
from io import BytesIO

from vpn_control_plane.data.models import NodeRecord
from vpn_control_plane.xui import XuiNodeClient

NodeBackupFile = tuple[str, bytes]


async def build_node_backup_archive(
    nodes: Sequence[NodeRecord],
    *,
    node_client_factory: Callable[[NodeRecord], XuiNodeClient] | None = None,
) -> bytes:
    files = await collect_node_backup_files(nodes, node_client_factory=node_client_factory)
    buffer = BytesIO()

    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in files:
            _add_bytes(archive, name, content)

    return buffer.getvalue()


async def collect_node_backup_files(
    nodes: Sequence[NodeRecord],
    *,
    node_client_factory: Callable[[NodeRecord], XuiNodeClient] | None = None,
) -> list[NodeBackupFile]:
    node_client_factory = node_client_factory or XuiNodeClient
    files: list[NodeBackupFile] = []
    errors: list[str] = []

    for node in nodes:
        node_client = node_client_factory(node)
        try:
            database = await node_client.get_database_backup()
            config = await node_client.get_config_json_backup()
        except Exception as exc:  # noqa: BLE001 - keep backups for other nodes available.
            errors.append(f"node {node.id} ({node.host}): {exc}")
            continue
        finally:
            close = getattr(node_client, "close", None)
            if close is not None:
                await close()

        prefix = f"{_backup_filename_part(node.host)}-{node.id}"
        files.append((f"{prefix}-x-ui.db", database))
        files.append((f"{prefix}-xray-config.json", config))

    if errors:
        files.append(("node-backup-errors.txt", ("\n".join(errors) + "\n").encode("utf-8")))
    return files


def _add_bytes(archive: tarfile.TarFile, name: str, content: bytes) -> None:
    info = tarfile.TarInfo(name=name)
    info.size = len(content)
    archive.addfile(info, BytesIO(content))


def _backup_filename_part(value: str) -> str:
    return "".join(char if char.isalnum() or char in {".", "-", "_"} else "_" for char in value).strip("._-") or "node"
