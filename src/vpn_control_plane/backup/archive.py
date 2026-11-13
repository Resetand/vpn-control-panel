from __future__ import annotations

import logging
import tarfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

from vpn_control_plane.backup import secrets
from vpn_control_plane.backup.data import BACKUP_DATA_FILE_NAME
from vpn_control_plane.backup.nodes import collect_node_backup_files
from vpn_control_plane.data.models import NodeRecord
from vpn_control_plane.xui import XuiNodeClient

logger = logging.getLogger(__name__)

# Every part of the backup is a directory of the same three files: a plain manifest and
# restore guide, and everything secret in one archive encrypted to the backup SSH key.
PART_NAME = "control-plane"
STATE_FILE_NAME = "state.tar.gz.age"
MANIFEST_FILE_NAME = "manifest.txt"
RESTORE_FILE_NAME = "RESTORE.md"
README_FILE_NAME = "README.md"
DECRYPT_SCRIPT_NAME = "decrypt.sh"
_RESERVED_NAMES = {PART_NAME, README_FILE_NAME, DECRYPT_SCRIPT_NAME}
_ASSETS = Path(__file__).parent / "assets"


@dataclass(frozen=True)
class FullBackup:
    file_name: str
    content: bytes


async def build_full_backup(
    data_file: Path,
    nodes: Sequence[NodeRecord],
    *,
    ssh_public_key: str | None,
    env_file: Path | None = None,
    external_subscriptions_file: Path | None = None,
    include_dir: Path | None = None,
    node_client_factory: Callable[[NodeRecord], XuiNodeClient] | None = None,
    now: datetime | None = None,
) -> FullBackup:
    if not ssh_public_key:
        raise secrets.SecretsBackupError(f"{secrets.BACKUP_SECRETS_ENV_KEY} is not set")
    now = now or datetime.now(UTC)
    root = f"vpn-backup-{now:%Y%m%d-%H%M}"

    state: list[tuple[str, bytes]] = []
    for path, name in (
        (data_file, BACKUP_DATA_FILE_NAME),
        (env_file, ".env"),
        (external_subscriptions_file, "external_subscriptions.resolved.json"),
    ):
        if path is not None and path.is_file():
            state.append((name, path.read_bytes()))
    node_files = await collect_node_backup_files(nodes, node_client_factory=node_client_factory)
    state.extend((f"nodes/{name}", content) for name, content in node_files)

    manifest = _manifest(
        now,
        ssh_public_key,
        nodes=",".join(f"{node.host}-{node.id}" for node in nodes),
        node_errors=str(sum(name == "node-backup-errors.txt" for name, _ in node_files)),
    )
    included = _list_included_files(include_dir) if include_dir is not None else []

    buffer = BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        _add_bytes(archive, f"{root}/{README_FILE_NAME}", _build_readme(now, included).encode(), now)
        _add_bytes(archive, f"{root}/{DECRYPT_SCRIPT_NAME}", (_ASSETS / DECRYPT_SCRIPT_NAME).read_bytes(), now, 0o755)
        part = f"{root}/{PART_NAME}"
        _add_bytes(archive, f"{part}/{MANIFEST_FILE_NAME}", manifest.encode(), now)
        _add_bytes(archive, f"{part}/{RESTORE_FILE_NAME}", (_ASSETS / RESTORE_FILE_NAME).read_bytes(), now)
        encrypted_state = secrets.encrypt_for_ssh_public_key(_tar_gz(state, now), ssh_public_key)
        _add_bytes(archive, f"{part}/{STATE_FILE_NAME}", encrypted_state, now)
        for arcname, path in included:
            archive.add(path, arcname=f"{root}/{arcname}")

    return FullBackup(file_name=f"{root}.tar.gz", content=buffer.getvalue())


def _manifest(now: datetime, ssh_public_key: str, **fields: str) -> str:
    lines = {
        "created_utc": f"{now:%Y-%m-%dT%H:%M:%SZ}",
        "recipient_fingerprint": secrets.ssh_public_key_fingerprint(ssh_public_key),
        "recipient_comment": secrets.ssh_public_key_comment(ssh_public_key),
        **fields,
    }
    return "".join(f"{key}={value}\n" for key, value in lines.items())


def _list_included_files(include_dir: Path) -> list[tuple[str, Path]]:
    """Parts other services leave for this backup; the archive carries them verbatim.

    Dot-prefixed entries are skipped so a producer can write a temp file and rename it.
    """
    if not include_dir.is_dir():
        return []
    files: list[tuple[str, Path]] = []
    for path in sorted(include_dir.rglob("*")):
        relative = path.relative_to(include_dir)
        if not path.is_file() or any(part.startswith(".") for part in relative.parts):
            continue
        if relative.parts[0] in _RESERVED_NAMES:
            logger.warning("Backup include %s skipped: the name belongs to the control plane", relative)
            continue
        files.append((relative.as_posix(), path))
    return files


def _build_readme(now: datetime, included: Sequence[tuple[str, Path]]) -> str:
    parts = [(PART_NAME, f"{now:%Y-%m-%d %H:%M} UTC")]
    for name in sorted({arcname.split("/")[0] for arcname, _ in included}):
        parts.append((name, _part_created(name, included)))
    rows = "".join(f"- `{name}/`, snapshot {created}\n" for name, created in parts)
    return f"""\
# VPN backup

Created {now:%Y-%m-%d %H:%M} UTC.

Each directory is one part with the same three files: `{MANIFEST_FILE_NAME}` (what and when, no
secrets), `{RESTORE_FILE_NAME}` (how to restore it) and `{STATE_FILE_NAME}` (everything secret,
encrypted with `age` to the backup SSH key, whose fingerprint is in the manifest).

## Decrypt

```sh
sh {DECRYPT_SCRIPT_NAME}               # finds the matching private key in ~/.ssh
sh {DECRYPT_SCRIPT_NAME} -i <key>      # or names it
```

Each part is decrypted into `<part>/state/`; then follow `<part>/{RESTORE_FILE_NAME}`.

## Parts

{rows}"""


def _part_created(name: str, included: Sequence[tuple[str, Path]]) -> str:
    """Snapshot time from the part's manifest, else the newest file time, so a stale part shows."""
    files = [path for arcname, path in included if arcname == name or arcname.startswith(f"{name}/")]
    for path in files:
        if path.name == MANIFEST_FILE_NAME:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("created_utc="):
                    return line.removeprefix("created_utc=")
    newest = max(path.stat().st_mtime for path in files)
    return f"{datetime.fromtimestamp(newest, UTC):%Y-%m-%d %H:%M} UTC (file time)"


def _tar_gz(files: Sequence[tuple[str, bytes]], now: datetime) -> bytes:
    buffer = BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in files:
            _add_bytes(archive, name, content, now)
    return buffer.getvalue()


def _add_bytes(archive: tarfile.TarFile, name: str, content: bytes, now: datetime, mode: int = 0o644) -> None:
    info = tarfile.TarInfo(name=name)
    info.size = len(content)
    info.mtime = int(now.timestamp())
    info.mode = mode
    archive.addfile(info, BytesIO(content))
