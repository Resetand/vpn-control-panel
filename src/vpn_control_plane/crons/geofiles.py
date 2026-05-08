from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Protocol

from vpn_control_plane.config import Settings
from vpn_control_plane.data import JsonStateStore, NodeRecord
from vpn_control_plane.xui import XuiNodeClient

logger = logging.getLogger(__name__)


class GeofileUpdateClient(Protocol):
    async def update_geofiles(self) -> None: ...

    async def close(self) -> None: ...


async def update_geofiles_on_all_nodes(
    store: JsonStateStore,
    *,
    client_factory: Callable[[NodeRecord], GeofileUpdateClient] = XuiNodeClient,
) -> None:
    nodes = store.load_nodes()
    if not nodes:
        logger.info("Geofiles update skipped: no nodes configured")
        return

    for node in nodes:
        client = client_factory(node)
        try:
            await client.update_geofiles()
        except Exception:
            logger.exception("Geofiles update failed", extra={"node_id": node.id})
        finally:
            await client.close()


async def update_geofiles(settings: Settings, store: JsonStateStore) -> None:
    await update_geofiles_on_all_nodes(store)


def register(app: Any, settings: Settings) -> None:
    if not settings.geofiles_update_enabled:
        logger.info("Geofiles update cron job is disabled")
        return
    app.add_cron_job(update_geofiles, settings.geofiles_update_schedule, name="geofiles update")
