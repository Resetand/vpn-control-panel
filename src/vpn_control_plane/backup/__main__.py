from __future__ import annotations

import argparse
from pathlib import Path

from vpn_control_plane.backup.data import write_data_backup
from vpn_control_plane.backup.secrets import BACKUP_SECRETS_ENV_KEY, write_encrypted_env_backup


def main() -> None:
    parser = argparse.ArgumentParser(description="Create VPN control-plane backup archives.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    data_parser = subparsers.add_parser("data", help="Create a control-plane JSON data backup.")
    data_parser.add_argument("--data-file", type=Path, required=True)
    data_parser.add_argument("--output", type=Path, required=True)

    secrets_parser = subparsers.add_parser("secrets", help="Create an encrypted .env secrets backup when configured.")
    secrets_parser.add_argument("--env-file", type=Path, required=True)
    secrets_parser.add_argument("--output", type=Path, required=True)

    args = parser.parse_args()
    if args.command == "data":
        output_path = write_data_backup(args.data_file, args.output)
        print(output_path)
        return

    secrets_output_path = write_encrypted_env_backup(args.env_file, args.output)
    if secrets_output_path is None:
        print(f"Skipped secrets backup: {BACKUP_SECRETS_ENV_KEY} is not configured")
        return
    print(secrets_output_path)


if __name__ == "__main__":
    main()
