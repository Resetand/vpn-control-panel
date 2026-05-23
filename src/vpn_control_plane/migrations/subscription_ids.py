from __future__ import annotations

import argparse
import os
from pathlib import Path

from vpn_control_plane.data import ClientRecord, JsonStateStore
from vpn_control_plane.provisioning import generate_subscription_id


def migrate_subscription_ids(store: JsonStateStore) -> tuple[int, int]:
    clients = store.load_clients()
    migrated = 0
    next_clients: list[ClientRecord] = []

    for client in clients:
        sub_id = client.sub_id if client.sub_id is not None and client.legacy_sub_id is not None else None
        if sub_id is None:
            sub_id = generate_subscription_id()
            migrated += 1

        next_clients.append(
            ClientRecord(
                id=client.id,
                comment=client.comment,
                subId=sub_id,
                legacySubId=client.legacy_sub_id or client.id,
            )
        )

    store.save_clients(next_clients)
    return migrated, len(next_clients)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fill subId and legacySubId for subscription links.")
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("VPN_DATA_DIR", "data"),
        help="directory with clients.json, nodes.json, inbounds.json, and subscription.json",
    )
    args = parser.parse_args()

    migrated, total = migrate_subscription_ids(JsonStateStore(Path(args.data_dir)))
    print(f"subscription ids migrated: {migrated}; clients total: {total}")


if __name__ == "__main__":
    main()
