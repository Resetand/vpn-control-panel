from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from vpn_control_plane.config import Settings
from vpn_control_plane.data import JsonStateStore, NodeRecord
from vpn_control_plane.monitoring.detectors import (
    ActiveCondition,
    detect_status_conditions,
    select_monitored_nodes,
    xui_unavailable_condition,
)
from vpn_control_plane.monitoring.notifier import TelegramAlertNotifier
from vpn_control_plane.monitoring.state import AlertCandidate, MonitoringAlertState
from vpn_control_plane.xui import XuiNodeClient, XuiNodeStatus

logger = logging.getLogger(__name__)


class StatusClient(Protocol):
    async def get_status(self) -> XuiNodeStatus: ...

    async def close(self) -> None: ...


class AlertNotifier(Protocol):
    async def send_alert(self, message: str) -> None: ...

    async def close(self) -> None: ...


class MonitoringService:
    def __init__(
        self,
        settings: Settings,
        store: JsonStateStore,
        *,
        notifier: AlertNotifier | None = None,
        client_factory: Callable[[NodeRecord], StatusClient] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._notifier = notifier or TelegramAlertNotifier(settings)
        self._client_factory = client_factory or (lambda node: XuiNodeClient(node))
        self._now = now or (lambda: datetime.now(UTC))
        self._state = MonitoringAlertState()

    async def run_forever(self) -> None:
        while True:
            await self.poll_once()
            await asyncio.sleep(self._settings.monitoring_poll_interval_seconds)

    async def poll_once(self) -> None:
        nodes = select_monitored_nodes(self._store.load_nodes())
        if not nodes:
            return
        now = self._now()
        await asyncio.gather(*(self._poll_node(node, now=now) for node in nodes))

    async def close(self) -> None:
        await self._notifier.close()

    async def _poll_node(self, node: NodeRecord, *, now: datetime) -> None:
        client = self._client_factory(node)
        try:
            status = await client.get_status()
            conditions = detect_status_conditions(
                status,
                cpu_threshold_percent=self._settings.monitoring_cpu_threshold_percent,
                ram_threshold_percent=self._settings.monitoring_ram_threshold_percent,
            )
        except Exception as exc:
            logger.warning("Monitoring status poll failed", extra={"node_id": node.id, "error": str(exc)})
            conditions = [xui_unavailable_condition(str(exc))]
        finally:
            await client.close()

        await self._deliver_candidates(node, conditions, now=now)

    async def _deliver_candidates(self, node: NodeRecord, conditions: list[ActiveCondition], *, now: datetime) -> None:
        candidates = self._state.update(
            node,
            conditions,
            now=now,
            failure_duration=timedelta(seconds=self._settings.monitoring_failure_duration_seconds),
            alert_cooldown=timedelta(seconds=self._settings.monitoring_alert_cooldown_seconds),
        )
        for candidate in candidates:
            message = format_alert_message(
                candidate,
                duration_seconds=self._settings.monitoring_failure_duration_seconds,
            )
            try:
                await self._notifier.send_alert(message)
            except Exception:
                logger.exception(
                    "Monitoring alert delivery failed",
                    extra={"node_id": node.id, "event_category": candidate.condition.category},
                )
                continue
            self._state.mark_alert_sent(candidate, now=now)


def format_alert_message(candidate: AlertCandidate, *, duration_seconds: int) -> str:
    node = candidate.node
    node_name = node.label or node.host
    lines = [
        f"Monitoring alert: {candidate.condition.title}",
        f"Node: {node_name} (id {node.id}, {node.host}:{node.port})",
        f"Category: {candidate.condition.category}",
        f"Duration: at least {duration_seconds}s",
    ]
    if candidate.condition.observed is not None:
        lines.append(f"Observed: {candidate.condition.observed}")
    if candidate.condition.threshold is not None:
        lines.append(f"Threshold: {candidate.condition.threshold}")
    return "\n".join(lines)


async def run_monitoring_alerts(settings: Settings, store: JsonStateStore) -> None:
    service = MonitoringService(settings, store)
    try:
        await service.run_forever()
    finally:
        await service.close()
