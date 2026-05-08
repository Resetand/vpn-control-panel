from __future__ import annotations

import tarfile
from collections.abc import Callable, Sequence
from io import BytesIO
from pathlib import Path

from vpn_control_plane.backup.data import BACKUP_DATA_FILES
from vpn_control_plane.backup.nodes import collect_node_backup_files
from vpn_control_plane.backup.secrets import ENCRYPTED_ENV_FILE_NAME, build_encrypted_env_backup
from vpn_control_plane.data.models import NodeRecord
from vpn_control_plane.xui import XuiNodeClient

CONTROL_PLANE_BACKUP_FILE_NAME = "vpn-control-plane-backup.tar.gz"


async def build_control_plane_backup(
    data_dir: Path,
    nodes: Sequence[NodeRecord],
    *,
    env_file: Path | None = None,
    ssh_public_key: str | None = None,
    node_client_factory: Callable[[NodeRecord], XuiNodeClient] | None = None,
) -> bytes:
    buffer = BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for file_name in BACKUP_DATA_FILES:
            file_path = data_dir / file_name
            if file_path.is_file():
                archive.add(file_path, arcname=f"data/{file_name}")

        if env_file is not None and ssh_public_key:
            _add_bytes(archive, ENCRYPTED_ENV_FILE_NAME, build_encrypted_env_backup(env_file, ssh_public_key))

        for name, content in await collect_node_backup_files(nodes, node_client_factory=node_client_factory):
            _add_bytes(archive, name, content)

    return buffer.getvalue()


def _add_bytes(archive: tarfile.TarFile, name: str, content: bytes) -> None:
    info = tarfile.TarInfo(name=name)
    info.size = len(content)
    archive.addfile(info, BytesIO(content))
