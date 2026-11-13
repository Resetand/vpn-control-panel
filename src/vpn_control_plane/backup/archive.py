from __future__ import annotations

import tarfile
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

from vpn_control_plane.backup.data import BACKUP_DATA_FILE_NAME
from vpn_control_plane.backup.nodes import collect_node_backup_files
from vpn_control_plane.backup.secrets import ENCRYPTED_ENV_FILE_NAME, build_encrypted_env_backup
from vpn_control_plane.data.models import NodeRecord
from vpn_control_plane.xui import XuiNodeClient

CONTROL_PLANE_BACKUP_FILE_NAME = "vpn-control-plane-backup.tar.gz"
BACKUP_README_FILE_NAME = "README.md"

_README_HEADER = """\
# VPN control plane backup

Created {created}.

- `data.json`: control-plane state (nodes, inbounds, clients).
  Restore in the control-plane checkout: `make restore-data BACKUP=<this archive>`, then `make start`.
- `env.encrypted`: control-plane `.env`, encrypted to `BACKUP_SECRETS_SSH_KEY`.
  `age -d -i ~/.ssh/id_ed25519 env.encrypted | tar -xzf -` writes `.env` to the current directory.
- `<host>-<id>-x-ui.db`: 3x-ui panel database of a node (inbounds, clients, panel settings).
  On the node with the same or newer 3x-ui: `systemctl stop x-ui`, replace `/etc/x-ui/x-ui.db`,
  delete `x-ui.db-wal` and `x-ui.db-shm` next to it, `systemctl start x-ui`.
- `<host>-<id>-xray-config.json`: Xray configuration the panel generated.
  Reference only: the panel rebuilds it from its database.
- `node-backup-errors.txt`: present only when some node files could not be fetched.
"""


async def build_control_plane_backup(
    data_file: Path,
    nodes: Sequence[NodeRecord],
    *,
    env_file: Path | None = None,
    ssh_public_key: str | None = None,
    include_dir: Path | None = None,
    node_client_factory: Callable[[NodeRecord], XuiNodeClient] | None = None,
) -> bytes:
    included = _list_included_files(include_dir) if include_dir is not None else []
    buffer = BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        _add_bytes(archive, BACKUP_README_FILE_NAME, _build_readme(included).encode("utf-8"))

        if data_file.is_file():
            archive.add(data_file, arcname=BACKUP_DATA_FILE_NAME)

        if env_file is not None and ssh_public_key:
            _add_bytes(archive, ENCRYPTED_ENV_FILE_NAME, build_encrypted_env_backup(env_file, ssh_public_key))

        for name, content in await collect_node_backup_files(nodes, node_client_factory=node_client_factory):
            _add_bytes(archive, name, content)

        for arcname, path in included:
            archive.add(path, arcname=arcname)

    return buffer.getvalue()


def _list_included_files(include_dir: Path) -> list[tuple[str, Path]]:
    """Files other services leave for this backup; the archive carries them verbatim.

    Dot-prefixed entries are skipped so a producer can write a temp file and rename it.
    """
    if not include_dir.is_dir():
        return []
    files: list[tuple[str, Path]] = []
    for path in sorted(include_dir.rglob("*")):
        relative = path.relative_to(include_dir)
        if path.is_file() and not any(part.startswith(".") for part in relative.parts):
            files.append((relative.as_posix(), path))
    return files


def _build_readme(included: Sequence[tuple[str, Path]]) -> str:
    readme = _README_HEADER.format(created=_utc(datetime.now(UTC)))
    if included:
        # Modification times make a producer that stopped updating its files visible.
        rows = "".join(
            f"- `{arcname}`, modified {_utc(datetime.fromtimestamp(path.stat().st_mtime, UTC))}\n"
            for arcname, path in included
        )
        readme += (
            "\n## Included from other services\n\n"
            "Copied as is; each service's directory carries its own restore guide.\n\n" + rows
        )
    return readme


def _utc(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M UTC")


def _add_bytes(archive: tarfile.TarFile, name: str, content: bytes) -> None:
    info = tarfile.TarInfo(name=name)
    info.size = len(content)
    archive.addfile(info, BytesIO(content))
