from __future__ import annotations

import tarfile
from io import BytesIO
from pathlib import Path

DATA_BACKUP_FILE_NAME = "vpn-control-plane-data.tar.gz"
BACKUP_DATA_FILE_NAME = "data.json"


def build_data_backup(data_file: Path) -> bytes:
    buffer = BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        if data_file.is_file():
            archive.add(data_file, arcname=BACKUP_DATA_FILE_NAME)
    return buffer.getvalue()


def write_data_backup(data_file: Path, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(build_data_backup(data_file))
    return output_path
