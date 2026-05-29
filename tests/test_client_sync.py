from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from vpn_control_plane.data import ControlPlaneStore, NodeRecord
from vpn_control_plane.provisioning import client_email
from vpn_control_plane.sync import ClientSyncService
from vpn_control_plane.xui import XuiClientInfo, XuiInbound

JsonObject = dict[str, Any]


class FakeNodeClient:
    def __init__(self, *, inbounds: list[XuiInbound], clients: list[XuiClientInfo]) -> None:
        self._inbounds = inbounds
        self._clients = clients
        self.list_inbounds_error: Exception | None = None
        self.added: list[tuple[JsonObject, list[int]]] = []
        self.attached: list[tuple[str, list[int]]] = []
        self.updated: list[tuple[str, JsonObject]] = []
        self.closed = False

    async def list_inbounds(self) -> list[XuiInbound]:
        if self.list_inbounds_error is not None:
            raise self.list_inbounds_error
        return self._inbounds

    async def list_clients(self) -> list[XuiClientInfo]:
        return self._clients

    async def add_client(self, client: JsonObject, inbound_ids: list[int]) -> None:
        self.added.append((client, inbound_ids))

    async def attach_client(self, email: str, inbound_ids: list[int]) -> None:
        self.attached.append((email, inbound_ids))

    async def update_client(self, email: str, client: JsonObject) -> None:
        self.updated.append((email, client))

    async def close(self) -> None:
        self.closed = True


def inbound(inbound_id: int, *, protocol: str = "vless", network: str = "tcp") -> XuiInbound:
    return XuiInbound(
        id=inbound_id,
        protocol=protocol,
        settings={},
        stream_settings={"network": network},
        sniffing={},
        raw={"id": inbound_id},
    )


def existing_client(
    client_id: str,
    *,
    inbound_ids: list[int],
    comment: str = "",
    sub_id: str = "",
    tg_id: int | None = None,
) -> XuiClientInfo:
    client: JsonObject = {
        "email": client_email(client_id),
        "comment": comment,
        "subId": sub_id,
        "uuid": f"uuid-{client_id}",
        "flow": "xtls-rprx-vision",
    }
    if tg_id is not None:
        client["tgId"] = tg_id
    return XuiClientInfo(client=client, inbound_ids=inbound_ids)


def build_state(clients: list[JsonObject]) -> JsonObject:
    return {
        "nodes": [
            {
                "id": 1,
                "host": "n1.example.test",
                "port": 443,
                "basePath": "/panel/",
                "apiToken": "t1",
                "inbounds": [
                    {"tag": "n1a", "label": "N1A", "xuiInboundId": 1},
                    {"tag": "n1b", "label": "N1B", "xuiInboundId": 2},
                ],
            },
            {
                "id": 2,
                "host": "n2.example.test",
                "port": 443,
                "basePath": "/panel/",
                "apiToken": "t2",
                "inbounds": [{"tag": "n2a", "label": "N2A", "xuiInboundId": 1}],
            },
        ],
        "externalInbounds": [],
        "clients": clients,
        "defaultClientInboundTags": ["n1a", "n2a"],
        "subscription": {},
    }


def prepare_store(tmp_path: Path, clients: list[JsonObject]) -> ControlPlaneStore:
    path = tmp_path / "data.json"
    path.write_text(json.dumps(build_state(clients)), encoding="utf-8")
    return ControlPlaneStore(path)


def make_service(
    store: ControlPlaneStore, fakes: dict[int, FakeNodeClient], *, dry_run: bool = False
) -> ClientSyncService:
    def factory(node: NodeRecord) -> FakeNodeClient:
        return fakes[node.id]

    return ClientSyncService(store, node_client_factory=factory, dry_run=dry_run)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_creates_missing_client_with_full_payload(tmp_path: Path) -> None:
    store = prepare_store(
        tmp_path,
        [{"id": "123", "telegramId": "123", "comment": "Alice", "subId": "sub-123", "inboundTags": ["n1a"]}],
    )
    node1 = FakeNodeClient(inbounds=[inbound(1), inbound(2)], clients=[])
    report = await make_service(store, {1: node1}).sync()

    assert report.created == 1
    assert report.updated == 0 and report.attached == 0
    payload, inbound_ids = node1.added[0]
    assert inbound_ids == [1]
    assert payload["email"] == client_email("123")
    assert payload["comment"] == "Alice"
    assert payload["subId"] == "sub-123"
    assert payload["tgId"] == 123
    assert payload["flow"] == "xtls-rprx-vision"
    assert node1.closed is True


@pytest.mark.asyncio
async def test_updates_existing_client_with_stale_fields_preserving_others(tmp_path: Path) -> None:
    store = prepare_store(
        tmp_path,
        [{"id": "123", "telegramId": "123", "comment": "Alice", "subId": "sub-new", "inboundTags": ["n1a"]}],
    )
    node1 = FakeNodeClient(
        inbounds=[inbound(1), inbound(2)],
        clients=[existing_client("123", inbound_ids=[1], comment="Old", sub_id="sub-old", tg_id=999)],
    )
    report = await make_service(store, {1: node1}).sync()

    assert report.updated == 1
    assert report.created == 0 and report.attached == 0
    assert node1.added == []
    email, payload = node1.updated[0]
    assert email == client_email("123")
    assert payload["comment"] == "Alice"
    assert payload["subId"] == "sub-new"
    assert payload["tgId"] == 123
    assert payload["uuid"] == "uuid-123"  # untouched field preserved


@pytest.mark.asyncio
async def test_no_op_when_panel_already_matches(tmp_path: Path) -> None:
    store = prepare_store(
        tmp_path,
        [{"id": "123", "telegramId": "123", "comment": "Alice", "subId": "sub-123", "inboundTags": ["n1a"]}],
    )
    node1 = FakeNodeClient(
        inbounds=[inbound(1)],
        clients=[existing_client("123", inbound_ids=[1], comment="Alice", sub_id="sub-123", tg_id=123)],
    )
    report = await make_service(store, {1: node1}).sync()

    assert (report.created, report.updated, report.attached) == (0, 0, 0)
    assert node1.updated == [] and node1.added == [] and node1.attached == []


@pytest.mark.asyncio
async def test_attaches_missing_inbounds_without_field_update(tmp_path: Path) -> None:
    store = prepare_store(
        tmp_path,
        [{"id": "123", "comment": "Alice", "subId": "sub-123", "inboundTags": ["n1a", "n1b"]}],
    )
    node1 = FakeNodeClient(
        inbounds=[inbound(1), inbound(2)],
        clients=[existing_client("123", inbound_ids=[1], comment="Alice", sub_id="sub-123", tg_id=123)],
    )
    report = await make_service(store, {1: node1}).sync()

    assert report.attached == 1
    assert report.updated == 0
    assert node1.attached == [(client_email("123"), [2])]


@pytest.mark.asyncio
async def test_leaves_panel_only_clients_untouched(tmp_path: Path) -> None:
    store = prepare_store(
        tmp_path,
        [{"id": "123", "comment": "Alice", "subId": "sub-123", "inboundTags": ["n1a"]}],
    )
    node1 = FakeNodeClient(
        inbounds=[inbound(1)],
        clients=[
            existing_client("123", inbound_ids=[1], comment="Alice", sub_id="sub-123"),
            existing_client("manual-999", inbound_ids=[1], comment="Hand-made"),
        ],
    )
    await make_service(store, {1: node1}).sync()

    touched_emails = {email for email, _ in node1.updated} | {client_email("manual-999")}
    assert all(email != client_email("manual-999") for email, _ in node1.updated)
    assert node1.added == []
    assert client_email("manual-999") in touched_emails  # sanity: it existed but was never written


@pytest.mark.asyncio
async def test_node_failure_is_recorded_and_other_nodes_still_sync(tmp_path: Path) -> None:
    store = prepare_store(
        tmp_path,
        [
            {"id": "123", "comment": "Alice", "subId": "sub-123", "inboundTags": ["n1a"]},
            {"id": "456", "comment": "Bob", "subId": "sub-456", "inboundTags": ["n2a"]},
        ],
    )
    node1 = FakeNodeClient(inbounds=[inbound(1)], clients=[])
    node1.list_inbounds_error = RuntimeError("node down")
    node2 = FakeNodeClient(inbounds=[inbound(1)], clients=[])

    report = await make_service(store, {1: node1, 2: node2}).sync()

    assert len(report.node_errors) == 1
    assert "node 1" in report.node_errors[0]
    assert report.created == 1
    assert len(node2.added) == 1
    assert node1.closed is True and node2.closed is True


@pytest.mark.asyncio
async def test_missing_inbound_on_node_is_recorded_without_writing(tmp_path: Path) -> None:
    store = prepare_store(
        tmp_path,
        [{"id": "123", "comment": "Alice", "subId": "sub-123", "inboundTags": ["n1b"]}],
    )
    node1 = FakeNodeClient(inbounds=[inbound(1)], clients=[])  # inbound id 2 (n1b) absent
    report = await make_service(store, {1: node1}).sync()

    assert report.created == 0
    assert node1.added == []
    assert len(report.node_errors) == 1
    assert "not found" in report.node_errors[0]


@pytest.mark.asyncio
async def test_dry_run_counts_changes_without_writing(tmp_path: Path) -> None:
    store = prepare_store(
        tmp_path,
        [
            {"id": "123", "comment": "Alice", "subId": "sub-123", "inboundTags": ["n1a"]},
            {"id": "456", "comment": "Bob", "subId": "sub-new", "inboundTags": ["n1a"]},
        ],
    )
    node1 = FakeNodeClient(
        inbounds=[inbound(1)],
        clients=[existing_client("456", inbound_ids=[1], comment="Bob", sub_id="sub-old")],
    )
    report = await make_service(store, {1: node1}, dry_run=True).sync()

    assert report.created == 1
    assert report.updated == 1
    assert node1.added == [] and node1.updated == [] and node1.attached == []


@pytest.mark.asyncio
async def test_telegram_id_falls_back_to_numeric_client_id(tmp_path: Path) -> None:
    store = prepare_store(
        tmp_path,
        [{"id": "200", "comment": "Carol", "subId": "sub-200", "inboundTags": ["n1a"]}],
    )
    node1 = FakeNodeClient(inbounds=[inbound(1)], clients=[])
    await make_service(store, {1: node1}).sync()

    payload, _ = node1.added[0]
    assert payload["tgId"] == 200
