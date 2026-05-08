from __future__ import annotations

import tarfile
from io import BytesIO
from pathlib import Path

DATA_BACKUP_FILE_NAME = "vpn-control-plane-data.tar.gz"
BACKUP_DATA_FILES = ("nodes.json", "clients.json", "inbounds.json", "subscription.json")


def build_data_backup(data_dir: Path) -> bytes:
    buffer = BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for file_name in BACKUP_DATA_FILES:
            file_path = data_dir / file_name
            if file_path.is_file():
                archive.add(file_path, arcname=file_name)
    return buffer.getvalue()


def write_data_backup(data_dir: Path, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(build_data_backup(data_dir))
    return output_path
