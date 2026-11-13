from __future__ import annotations

import json
import os
import subprocess
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from vpn_control_plane.backup import SecretsBackupError, build_full_backup
from vpn_control_plane.backup.secrets import ssh_public_key_fingerprint
from vpn_control_plane.data import NodeRecord

NOW = datetime(2026, 11, 13, 0, 10, tzinfo=UTC)
ROOT = "vpn-backup-20261113-0010"


def node(node_id: int, host: str) -> NodeRecord:
    return NodeRecord.model_validate(
        {"id": node_id, "host": host, "port": 2053, "basePath": "panel", "apiToken": "token"}
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


def generate_key(directory: Path, name: str = "id_ed25519") -> tuple[Path, str]:
    directory.mkdir(parents=True, exist_ok=True)
    key = directory / name
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "backup@test", "-f", str(key)], check=True)
    return key, (directory / f"{name}.pub").read_text(encoding="utf-8").strip()


def extract(content: bytes, target: Path) -> Path:
    target.mkdir()
    archive_path = target / "backup.tar.gz"
    archive_path.write_bytes(content)
    with tarfile.open(archive_path, mode="r:gz") as archive:
        archive.extractall(target, filter="data")
    return target / ROOT


def run_decrypt(root: Path, home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(root / "decrypt.sh"), *args],
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(home)},
        check=False,
    )


@pytest.fixture
def control_plane_files(tmp_path: Path) -> dict[str, Path]:
    data_file = tmp_path / "data" / "data.json"
    data_file.parent.mkdir()
    data_file.write_text(json.dumps({"nodes": [], "clients": []}), encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text("VPN_TELEGRAM_BOT_TOKEN=secret\n", encoding="utf-8")
    resolved = tmp_path / "data" / "external_subscriptions.resolved.json"
    resolved.write_text("{}", encoding="utf-8")
    include_dir = tmp_path / "include"
    (include_dir / "monitoring").mkdir(parents=True)
    (include_dir / "monitoring" / ".state.tar.gz.age.tmp").write_bytes(b"half-written")
    (include_dir / "control-plane").mkdir()
    (include_dir / "control-plane" / "manifest.txt").write_text("spoofed", encoding="utf-8")
    return {"data": data_file, "env": env_file, "resolved": resolved, "include": include_dir}


def add_monitoring_part(include_dir: Path, public_key: str, work: Path) -> None:
    """A part the way another service leaves it: its own state, encrypted with age to the key."""
    work.mkdir()
    (work / "grafana.db").write_bytes(b"grafana")
    subprocess.run(["tar", "-czf", str(work / "state.tar.gz"), "-C", str(work), "grafana.db"], check=True)
    (work / "recipient.pub").write_text(public_key + "\n", encoding="utf-8")
    part = include_dir / "monitoring"
    subprocess.run(
        ["age", "-R", str(work / "recipient.pub"), "-o", str(part / "state.tar.gz.age"), str(work / "state.tar.gz")],
        check=True,
    )
    (part / "manifest.txt").write_text(
        f"created_utc=2026-11-12T23:04:00Z\nrecipient_fingerprint={ssh_public_key_fingerprint(public_key)}\n",
        encoding="utf-8",
    )


async def build(files: dict[str, Path], ssh_public_key: str | None) -> bytes:
    backup = await build_full_backup(
        files["data"],
        [node(1, "eu.example.test")],
        ssh_public_key=ssh_public_key,
        env_file=files["env"],
        external_subscriptions_file=files["resolved"],
        include_dir=files["include"],
        node_client_factory=cast(Any, FakeXuiNodeClient),
        now=NOW,
    )
    assert backup.file_name == f"{ROOT}.tar.gz"
    return backup.content


@pytest.mark.asyncio
async def test_full_backup_has_uniform_parts_and_decrypts_with_the_key_found_in_ssh_dir(
    tmp_path: Path, control_plane_files: dict[str, Path]
) -> None:
    home = tmp_path / "home"
    _key, public_key = generate_key(home / ".ssh")
    add_monitoring_part(control_plane_files["include"], public_key, tmp_path / "monitoring-work")

    root = extract(await build(control_plane_files, public_key), tmp_path / "out")

    names = sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())
    assert names == [
        "README.md",
        "control-plane/RESTORE.md",
        "control-plane/manifest.txt",
        "control-plane/state.tar.gz.age",
        "decrypt.sh",
        "monitoring/manifest.txt",
        "monitoring/state.tar.gz.age",
    ]
    manifest = (root / "control-plane" / "manifest.txt").read_text(encoding="utf-8")
    assert f"recipient_fingerprint={ssh_public_key_fingerprint(public_key)}\n" in manifest
    assert "recipient_comment=backup@test\n" in manifest
    assert "nodes=eu.example.test-1\n" in manifest
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert "`control-plane/`, snapshot 2026-11-13 00:10 UTC" in readme
    assert "`monitoring/`, snapshot 2026-11-12T23:04:00Z" in readme

    result = run_decrypt(root, home)

    assert result.returncode == 0, result.stderr
    state = root / "control-plane" / "state"
    assert (state / "data.json").read_bytes() == control_plane_files["data"].read_bytes()
    assert (state / ".env").read_bytes() == control_plane_files["env"].read_bytes()
    assert (state / "external_subscriptions.resolved.json").read_text(encoding="utf-8") == "{}"
    assert (state / "nodes" / "eu.example.test-1-x-ui.db").read_bytes() == b"db-1"
    assert (state / "nodes" / "eu.example.test-1-xray-config.json").read_bytes() == b"config-1"
    assert (root / "monitoring" / "state" / "grafana.db").read_bytes() == b"grafana"


@pytest.mark.asyncio
async def test_decrypt_takes_an_explicit_key_and_names_the_missing_one(
    tmp_path: Path, control_plane_files: dict[str, Path]
) -> None:
    key, public_key = generate_key(tmp_path / "elsewhere")
    generate_key(tmp_path / "home" / ".ssh")
    root = extract(await build(control_plane_files, public_key), tmp_path / "out")

    missing = run_decrypt(root, tmp_path / "home")
    explicit = run_decrypt(root, tmp_path / "home", "-i", str(key))

    assert missing.returncode == 1
    assert ssh_public_key_fingerprint(public_key) in missing.stderr
    assert "backup@test" in missing.stderr
    assert explicit.returncode == 0, explicit.stderr
    assert (root / "control-plane" / "state" / "data.json").is_file()


@pytest.mark.asyncio
async def test_decrypt_restores_the_parts_it_can_and_names_the_rest(
    tmp_path: Path, control_plane_files: dict[str, Path]
) -> None:
    home = tmp_path / "home"
    _key, public_key = generate_key(home / ".ssh")
    _other_key, other_public_key = generate_key(tmp_path / "other")
    add_monitoring_part(control_plane_files["include"], other_public_key, tmp_path / "monitoring-work")
    root = extract(await build(control_plane_files, public_key), tmp_path / "out")

    result = run_decrypt(root, home)

    assert result.returncode == 1
    assert "not decrypted: monitoring" in result.stderr
    assert (root / "control-plane" / "state" / "data.json").is_file()


def test_fingerprint_matches_ssh_keygen(tmp_path: Path) -> None:
    key, public_key = generate_key(tmp_path)
    listed = subprocess.run(["ssh-keygen", "-l", "-f", str(key)], capture_output=True, text=True, check=True)

    assert listed.stdout.split()[1] == ssh_public_key_fingerprint(public_key)


@pytest.mark.asyncio
async def test_full_backup_requires_the_backup_ssh_key(control_plane_files: dict[str, Path]) -> None:
    with pytest.raises(SecretsBackupError, match="BACKUP_SECRETS_SSH_KEY"):
        await build(control_plane_files, None)
