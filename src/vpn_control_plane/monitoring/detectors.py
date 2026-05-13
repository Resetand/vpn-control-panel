from __future__ import annotations

from dataclasses import dataclass

from vpn_control_plane.data import NodeRecord
from vpn_control_plane.xui import XuiNodeStatus

EVENT_XUI_UNAVAILABLE = "xui_unavailable"
EVENT_XRAY_DOWN = "xray_down"
EVENT_CPU_HIGH = "cpu_high"
EVENT_RAM_HIGH = "ram_high"


@dataclass(frozen=True)
class ActiveCondition:
    category: str
    title: str
    observed: str | None = None
    threshold: str | None = None


def select_monitored_nodes(nodes: list[NodeRecord]) -> list[NodeRecord]:
    return [node for node in nodes if node.monitoring]


def xui_unavailable_condition(reason: str) -> ActiveCondition:
    return ActiveCondition(category=EVENT_XUI_UNAVAILABLE, title="3x-UI unavailable", observed=reason)


def detect_status_conditions(
    status: XuiNodeStatus,
    *,
    cpu_threshold_percent: float,
    ram_threshold_percent: float,
) -> list[ActiveCondition]:
    conditions: list[ActiveCondition] = []
    if status.xray.state != "running":
        conditions.append(
            ActiveCondition(
                category=EVENT_XRAY_DOWN,
                title="Xray is not running",
                observed=status.xray.state,
                threshold="running",
            )
        )
    if status.cpu_percent > cpu_threshold_percent:
        conditions.append(
            ActiveCondition(
                category=EVENT_CPU_HIGH,
                title="CPU usage is high",
                observed=f"{status.cpu_percent:.1f}%",
                threshold=f"> {cpu_threshold_percent:.1f}%",
            )
        )
    ram_percent = status.memory.usage_percent
    if ram_percent > ram_threshold_percent:
        conditions.append(
            ActiveCondition(
                category=EVENT_RAM_HIGH,
                title="RAM usage is high",
                observed=f"{ram_percent:.1f}%",
                threshold=f"> {ram_threshold_percent:.1f}%",
            )
        )
    return conditions
