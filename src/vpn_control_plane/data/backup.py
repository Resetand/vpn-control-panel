from __future__ import annotations

import argparse
import subprocess
import tarfile
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory

from dotenv import dotenv_values

BACKUP_FILE_NAME = "vpn-control-plane-data.tar.gz"
BACKUP_DATA_FILES = ("nodes.json", "clients.json", "inbounds.json", "subscription.json")
SECRETS_BACKUP_FILE_NAME = "backup.secrets"
SECRETS_BACKUP_ENV_KEY = "BACKUP_SECRETS_SSH_KEY"


class SecretsBackupError(RuntimeError):
    pass


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


def build_secrets_backup(env_file: Path, ssh_public_key: str) -> bytes:
    if not ssh_public_key.strip():
        raise SecretsBackupError(f"{SECRETS_BACKUP_ENV_KEY} is empty")
    if not env_file.is_file():
        raise SecretsBackupError(f"secrets source file does not exist: {env_file}")

    plaintext = _build_secrets_archive(env_file)
    return encrypt_for_ssh_public_key(plaintext, ssh_public_key)


def write_secrets_backup(env_file: Path, output_path: Path, ssh_public_key: str | None = None) -> Path | None:
    ssh_public_key = ssh_public_key if ssh_public_key is not None else load_backup_secrets_ssh_key(env_file)
    if not ssh_public_key:
        return None
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(build_secrets_backup(env_file, ssh_public_key))
    return output_path


def load_backup_secrets_ssh_key(env_file: Path) -> str | None:
    if not env_file.is_file():
        return None
    value = dotenv_values(env_file).get(SECRETS_BACKUP_ENV_KEY)
    if value is None:
        return None
    value = value.strip()
    return value or None


def encrypt_for_ssh_public_key(plaintext: bytes, ssh_public_key: str) -> bytes:
    with TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        recipient_file = temp_path / "recipient.pub"
        plaintext_file = temp_path / "secrets.tar.gz"
        recipient_file.write_text(ssh_public_key.strip() + "\n", encoding="utf-8")
        plaintext_file.write_bytes(plaintext)
        try:
            result = subprocess.run(
                ["age", "-R", str(recipient_file), "-o", "-", str(plaintext_file)],
                check=True,
                capture_output=True,
            )
        except FileNotFoundError as exc:
            raise SecretsBackupError("age command is required to encrypt secrets backups") from exc
        except subprocess.CalledProcessError as exc:
            message = exc.stderr.decode("utf-8", errors="replace").strip() or "age encryption failed"
            raise SecretsBackupError(message) from exc
    return result.stdout


def _build_secrets_archive(env_file: Path) -> bytes:
    buffer = BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        archive.add(env_file, arcname=".env")
    return buffer.getvalue()


def main() -> None:
    parser = argparse.ArgumentParser(description="Create VPN control-plane backup archives.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    data_parser = subparsers.add_parser("data", help="Create a control-plane JSON data backup.")
    data_parser.add_argument("--data-dir", type=Path, required=True)
    data_parser.add_argument("--output", type=Path, required=True)

    secrets_parser = subparsers.add_parser("secrets", help="Create an encrypted .env secrets backup when configured.")
    secrets_parser.add_argument("--env-file", type=Path, required=True)
    secrets_parser.add_argument("--output", type=Path, required=True)

    args = parser.parse_args()
    if args.command == "data":
        output_path = write_data_backup(args.data_dir, args.output)
        print(output_path)
        return

    secrets_output_path = write_secrets_backup(args.env_file, args.output)
    if secrets_output_path is None:
        print(f"Skipped secrets backup: {SECRETS_BACKUP_ENV_KEY} is not configured")
        return
    print(secrets_output_path)


if __name__ == "__main__":
    main()