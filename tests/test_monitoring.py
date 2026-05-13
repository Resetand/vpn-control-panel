from __future__ import annotations

import asyncio
import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import vpn_control_plane.app as app_module
from vpn_control_plane.config import Settings
from vpn_control_plane.data import JsonStateStore, NodeRecord
from vpn_control_plane.monitoring import ActiveCondition, MonitoringAlertState, MonitoringService
from vpn_control_plane.xui import XuiMemoryStatus, XuiNodeStatus, XuiXrayStatus


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def node_payload(node_id: int, *, monitoring: bool | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": node_id,
        "host": f"node-{node_id}.example.test",
        "port": 443,
        "apiToken": f"token-{node_id}",
        "label": f"Node {node_id}",
    }
    if monitoring is not None:
        payload["monitoring"] = monitoring
    return payload


def prepare_data_dir(tmp_path: Path, *, nodes: list[dict[str, object]] | None = None) -> JsonStateStore:
    write_json(tmp_path / "nodes.json", nodes or [node_payload(1)])
    write_json(tmp_path / "clients.json", [])
    write_json(tmp_path / "inbounds.json", [])
    write_json(tmp_path / "subscription.json", {})
    return JsonStateStore(tmp_path)


def settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "VPN_DATA_DIR": str(tmp_path),
        "VPN_TELEGRAM_BOT_TOKEN": "token",
        "VPN_TELEGRAM_ADMIN_IDS": "1",
        "VPN_MONITORING_ALERTS_ENABLED": "true",
        "VPN_MONITORING_FAILURE_DURATION_SECONDS": "60",
        "VPN_MONITORING_ALERT_COOLDOWN_SECONDS": "3600",
    }
    values.update(overrides)
    return Settings.model_validate(values)


def node(node_id: int = 1, *, monitoring: bool = True) -> NodeRecord:
    return NodeRecord.model_validate(node_payload(node_id, monitoring=monitoring))


def status(
    *,
    cpu: float = 5.0,
    mem_current: int = 10,
    mem_total: int = 100,
    xray_state: str = "running",
) -> XuiNodeStatus:
    return XuiNodeStatus(
        cpu_percent=cpu,
        memory=XuiMemoryStatus(current=mem_current, total=mem_total),
        xray=XuiXrayStatus(state=xray_state, error_message="", version="26.4.25"),
        raw={},
    )


class FakeStatusClient:
    def __init__(self, result: XuiNodeStatus | Exception) -> None:
        self.result = result
        self.closed = False

    async def get_status(self) -> XuiNodeStatus:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def close(self) -> None:
        self.closed = True


class FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.closed = False

    async def send_alert(self, message: str) -> None:
        self.messages.append(message)

    async def close(self) -> None:
        self.closed = True


def test_monitoring_state_requires_sustained_condition_and_suppresses_cooldown() -> None:
    alert_state = MonitoringAlertState()
    started_at = datetime(2026, 5, 13, tzinfo=UTC)
    condition = ActiveCondition(category="cpu_high", title="CPU usage is high", observed="95.0%", threshold="> 90.0%")
    monitored_node = node()

    assert (
        alert_state.update(
            monitored_node,
            [condition],
            now=started_at,
            failure_duration=timedelta(seconds=60),
            alert_cooldown=timedelta(hours=1),
        )
        == []
    )

    candidates = alert_state.update(
        monitored_node,
        [condition],
        now=started_at + timedelta(seconds=60),
        failure_duration=timedelta(seconds=60),
        alert_cooldown=timedelta(hours=1),
    )
    assert len(candidates) == 1
    alert_state.mark_alert_sent(candidates[0], now=started_at + timedelta(seconds=60))

    assert (
        alert_state.update(
            monitored_node,
            [condition],
            now=started_at + timedelta(minutes=30),
            failure_duration=timedelta(seconds=60),
            alert_cooldown=timedelta(hours=1),
        )
        == []
    )
    assert (
        len(
            alert_state.update(
                monitored_node,
                [condition],
                now=started_at + timedelta(hours=2),
                failure_duration=timedelta(seconds=60),
                alert_cooldown=timedelta(hours=1),
            )
        )
        == 1
    )


def test_monitoring_state_resets_condition_that_clears_before_duration() -> None:
    alert_state = MonitoringAlertState()
    started_at = datetime(2026, 5, 13, tzinfo=UTC)
    condition = ActiveCondition(category="xray_down", title="Xray is not running", observed="stopped")
    monitored_node = node()

    alert_state.update(
        monitored_node,
        [condition],
        now=started_at,
        failure_duration=timedelta(seconds=60),
        alert_cooldown=timedelta(hours=1),
    )
    assert (
        alert_state.update(
            monitored_node,
            [],
            now=started_at + timedelta(seconds=30),
            failure_duration=timedelta(seconds=60),
            alert_cooldown=timedelta(hours=1),
        )
        == []
    )
    assert (
        alert_state.update(
            monitored_node,
            [condition],
            now=started_at + timedelta(seconds=61),
            failure_duration=timedelta(seconds=60),
            alert_cooldown=timedelta(hours=1),
        )
        == []
    )


@pytest.mark.asyncio
async def test_monitoring_poll_skips_nodes_with_monitoring_disabled(tmp_path: Path) -> None:
    store = prepare_data_dir(tmp_path, nodes=[node_payload(1), node_payload(2, monitoring=False)])
    polled_node_ids: list[int] = []
    clients: list[FakeStatusClient] = []

    def client_factory(monitored_node: NodeRecord) -> FakeStatusClient:
        polled_node_ids.append(monitored_node.id)
        client = FakeStatusClient(status())
        clients.append(client)
        return client

    service = MonitoringService(settings(tmp_path), store, notifier=FakeNotifier(), client_factory=client_factory)

    await service.poll_once()

    assert polled_node_ids == [1]
    assert all(client.closed for client in clients)


@pytest.mark.asyncio
async def test_monitoring_poll_alerts_for_sustained_status_failure(tmp_path: Path) -> None:
    store = prepare_data_dir(tmp_path)
    notifier = FakeNotifier()
    now_values = iter(
        [
            datetime(2026, 5, 13, tzinfo=UTC),
            datetime(2026, 5, 13, tzinfo=UTC) + timedelta(seconds=60),
        ]
    )

    def client_factory(_node: NodeRecord) -> FakeStatusClient:
        return FakeStatusClient(RuntimeError("connection failed"))

    service = MonitoringService(
        settings(tmp_path),
        store,
        notifier=notifier,
        client_factory=client_factory,
        now=lambda: next(now_values),
    )

    await service.poll_once()
    await service.poll_once()

    assert len(notifier.messages) == 1
    assert "3x-UI unavailable" in notifier.messages[0]
    assert "Category: xui_unavailable" in notifier.messages[0]


@pytest.mark.asyncio
async def test_monitoring_poll_alerts_for_xray_cpu_and_ram(tmp_path: Path) -> None:
    store = prepare_data_dir(tmp_path)
    notifier = FakeNotifier()
    now_values = iter(
        [
            datetime(2026, 5, 13, tzinfo=UTC),
            datetime(2026, 5, 13, tzinfo=UTC) + timedelta(seconds=60),
        ]
    )

    def client_factory(_node: NodeRecord) -> FakeStatusClient:
        return FakeStatusClient(status(cpu=95.0, mem_current=95, mem_total=100, xray_state="stopped"))

    service = MonitoringService(
        settings(tmp_path),
        store,
        notifier=notifier,
        client_factory=client_factory,
        now=lambda: next(now_values),
    )

    await service.poll_once()
    await service.poll_once()

    assert len(notifier.messages) == 3
    joined_messages = "\n".join(notifier.messages)
    assert "Category: xray_down" in joined_messages
    assert "Category: cpu_high" in joined_messages
    assert "Observed: 95.0%" in joined_messages
    assert "Category: ram_high" in joined_messages
    assert "Threshold: > 90.0%" in joined_messages


def test_app_does_not_start_monitoring_when_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prepare_data_dir(tmp_path)
    called = False

    async def fake_run_monitoring_alerts(_settings: Settings, _store: JsonStateStore) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(app_module, "run_monitoring_alerts", fake_run_monitoring_alerts)
    app = app_module.create_app(settings(tmp_path, VPN_MONITORING_ALERTS_ENABLED="false"), start_telegram=False)

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200

    assert called is False


def test_app_starts_monitoring_when_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prepare_data_dir(tmp_path)
    started = threading.Event()

    async def fake_run_monitoring_alerts(_settings: Settings, _store: JsonStateStore) -> None:
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(app_module, "run_monitoring_alerts", fake_run_monitoring_alerts)
    app = app_module.create_app(settings(tmp_path), start_telegram=False)

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert started.wait(timeout=1)
