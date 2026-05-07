from __future__ import annotations

import tarfile
from io import BytesIO
from pathlib import Path

import pytest

import vpn_control_plane.data.backup as backup_module
from vpn_control_plane.data import write_secrets_backup


def test_secrets_backup_is_skipped_without_configured_ssh_key(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    output_path = tmp_path / "backup.secrets"
    env_file.write_text("VPN_TELEGRAM_BOT_TOKEN=secret\n", encoding="utf-8")

    result = write_secrets_backup(env_file, output_path)

    assert result is None
    assert not output_path.exists()


def test_secrets_backup_encrypts_env_file_with_configured_ssh_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = tmp_path / ".env"
    output_path = tmp_path / "backup.secrets"
    env_file.write_text(
        'BACKUP_SECRETS_SSH_KEY="ssh-ed25519 AAAATEST backup"\nVPN_TELEGRAM_BOT_TOKEN=secret\n',
        encoding="utf-8",
    )
    captured: dict[str, str] = {}

    def fake_encrypt(plaintext: bytes, ssh_public_key: str) -> bytes:
        captured["ssh_public_key"] = ssh_public_key
        with tarfile.open(fileobj=BytesIO(plaintext), mode="r:gz") as archive:
            env_member = archive.extractfile(".env")
            assert env_member is not None
            captured["env"] = env_member.read().decode("utf-8")
        return b"encrypted-secrets"

    monkeypatch.setattr(backup_module, "encrypt_for_ssh_public_key", fake_encrypt)

    result = write_secrets_backup(env_file, output_path)

    assert result == output_path
    assert output_path.read_bytes() == b"encrypted-secrets"
    assert captured == {
        "ssh_public_key": "ssh-ed25519 AAAATEST backup",
        "env": 'BACKUP_SECRETS_SSH_KEY="ssh-ed25519 AAAATEST backup"\nVPN_TELEGRAM_BOT_TOKEN=secret\n',
    }