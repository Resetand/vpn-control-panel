from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from vpn_control_plane.config import Settings
from vpn_control_plane.data.store import ControlPlaneStore
from vpn_control_plane.sync.service import ClientSyncService


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description="Reconcile data.json clients onto the 3x-ui nodes.")
    parser.add_argument("--data-file", type=Path, default=None, help="Override the data file from settings.")
    parser.add_argument("--dry-run", action="store_true", help="Log intended changes without writing to the panels.")
    args = parser.parse_args()

    settings = Settings()  # type: ignore[call-arg]
    store = ControlPlaneStore(args.data_file or settings.data_file)
    store.verify_ready()

    service = ClientSyncService(
        store,
        default_vless_flow=settings.default_vless_flow,
        dry_run=args.dry_run,
    )
    report = asyncio.run(service.sync())
    print(report.summary)
    if report.node_errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
