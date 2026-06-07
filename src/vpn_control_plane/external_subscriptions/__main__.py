from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from vpn_control_plane.config import Settings
from vpn_control_plane.data.store import ControlPlaneStore
from vpn_control_plane.external_subscriptions.service import ExternalSubscriptionService


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description="Fetch external subscriptions and write the resolved-inbounds file.")
    parser.add_argument("--data-file", type=Path, default=None, help="Override the data file from settings.")
    args = parser.parse_args()

    settings = Settings()  # type: ignore[call-arg]
    if args.data_file is not None:
        settings = settings.model_copy(update={"data_file": args.data_file})
    store = ControlPlaneStore(settings.data_file)
    store.verify_ready()

    service = ExternalSubscriptionService(settings, store)
    resolved = asyncio.run(service.refresh_all())

    print(f"Resolved inbounds written to {settings.external_subscriptions_resolved_path}")
    for name, entries in resolved.items():
        print(f"  {name}: {len(entries)} inbounds")


if __name__ == "__main__":
    main()
