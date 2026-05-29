from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import pytest

from vpn_control_plane.data import ControlPlaneStore, NodeRecord
from vpn_control_plane.provisioning import ProvisioningService, client_email, telegram_client_id
from vpn_control_plane.xui import XuiClientInfo, XuiInbound

JsonObject = dict[str, Any]
SUBSCRIPTION_ID = "eHh4eHh4eHh4eHh4eHh4eHh4"


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def prepare_store(
    tmp_path: Path,
    *,
    clients: list[JsonObject] | None = None,
    inbounds: list[JsonObject] | None = None,
) -> ControlPlaneStore:
    state = build_state(clients=clients or [], inbounds=inbounds)
    write_json(tmp_path / "data.json", state)
    return ControlPlaneStore(tmp_path / "data.json")


def build_state(
    *,
    clients: list[JsonObject],
    inbounds: list[JsonObject] | None = None,
) -> JsonObject:
    raw_inbounds = inbounds or [
        {"label": "One", "nodeId": 1, "xuiInboundId": 1},
        {"label": "External", "uri": "vless://external#External"},
        {"label": "Two", "nodeId": 2, "xuiInboundId": 2},
    ]
    node_inbounds_by_id: dict[int, list[JsonObject]] = {1: [], 2: []}
    external_inbounds: list[JsonObject] = []
    default_tags: list[str] = []
    for index, item in enumerate(raw_inbounds):
        if "uri" in item:
            tag = str(item.get("tag") or f"external-{index}")
            external_inbounds.append({"tag": tag, "label": item["label"], "uri": item["uri"]})
        else:
            tag = str(item.get("tag") or f"node-{item['nodeId']}-{item['xuiInboundId']}-{index}")
            node_inbounds_by_id[int(item["nodeId"])].append(
                {"tag": tag, "label": item["label"], "xuiInboundId": item["xuiInboundId"]}
            )
        default_tags.append(tag)

    return {
        "nodes": [
            {
                "id": 1,
                "host": "node-1.example.test",
                "port": 443,
                "basePath": "/panel/",
                "apiToken": "token-1",
                "inbounds": node_inbounds_by_id[1],
            },
            {
                "id": 2,
                "host": "node-2.example.test",
                "port": 443,
                "basePath": "/panel/",
                "apiToken": "token-2",
                "inbounds": node_inbounds_by_id[2],
            },
        ],
        "externalInbounds": external_inbounds,
        "clients": clients,
        "defaultClientInboundTags": default_tags,
        "subscription": {},
    }


def inbound(inbound_id: int, protocol: str = "vless", *, network: str = "tcp") -> XuiInbound:
    return XuiInbound(
        id=inbound_id,
        protocol=protocol,
        settings={},
        stream_settings={"network": network},
        sniffing={},
        raw={},
    )


class FakeXuiClient:
    """Fake XuiNodeClient that simulates 3x-ui v3.2.0 client-first API."""

    def __init__(
        self,
        node: NodeRecord,
        available_inbounds: list[XuiInbound],
        initial_clients: dict[str, XuiClientInfo] | None = None,
    ) -> None:
        self.node = node
        self._available_inbounds = available_inbounds
        self._clients: dict[str, XuiClientInfo] = dict(initial_clients or {})
        self.add_calls: list[tuple[JsonObject, list[int]]] = []
        self.attach_calls: list[tuple[str, list[int]]] = []
        self.closed = False

    async def list_inbounds(self) -> list[XuiInbound]:
        return self._available_inbounds

    async def get_client(self, email: str) -> XuiClientInfo | None:
        return self._clients.get(email)

    async def add_client(self, client: JsonObject, inbound_ids: list[int]) -> None:
        self.add_calls.append((client, inbound_ids))
        email = str(client.get("email", ""))
        self._clients[email] = XuiClientInfo(client=client, inbound_ids=list(inbound_ids))

    async def attach_client(self, email: str, inbound_ids: list[int]) -> None:
        self.attach_calls.append((email, inbound_ids))
        existing = self._clients.get(email)
        if existing is not None:
            merged = sorted(set(existing.inbound_ids) | set(inbound_ids))
            self._clients[email] = XuiClientInfo(client=existing.client, inbound_ids=merged)

    async def close(self) -> None:
        self.closed = True


def service_with_fakes(
    store: ControlPlaneStore,
    inbounds_by_node: dict[int, list[XuiInbound]],
    initial_clients_by_node: dict[int, dict[str, XuiClientInfo]] | None = None,
) -> tuple[ProvisioningService, dict[int, FakeXuiClient]]:
    fake_clients: dict[int, FakeXuiClient] = {}
    initial_clients_by_node = initial_clients_by_node or {}

    def factory(node: NodeRecord) -> FakeXuiClient:
        client = fake_clients.get(node.id)
        if client is None:
            client = FakeXuiClient(
                node, inbounds_by_node[node.id], initial_clients_by_node.get(node.id)
            )
            fake_clients[node.id] = client
        return client

    service = ProvisioningService(
        store,
        node_client_factory=cast(Any, factory),
        subscription_id_factory=lambda: SUBSCRIPTION_ID,
    )
    return service, fake_clients


@pytest.mark.asyncio
async def test_selects_control_plane_ids_and_email() -> None:
    assert telegram_client_id(123456789) == "123456789"
    assert client_email("123456789") == "123456789"


@pytest.mark.asyncio
async def test_new_client_provisioning_adds_to_all_nodes_and_persists_record(tmp_path: Path) -> None:
    store = prepare_store(tmp_path)
    service, clients = service_with_fakes(store, {1: [inbound(1, "vless")], 2: [inbound(2, "vmess")]})

    result = await service.ensure_telegram_user(123, comment="Kirill", username="resetand")

    assert result.client.id == "123"
    assert result.client.effective_sub_id == SUBSCRIPTION_ID
    assert result.created == 2
    # Node 1: one add_client call with all target inbound ids
    assert len(clients[1].add_calls) == 1
    payload_1, ids_1 = clients[1].add_calls[0]
    assert payload_1["email"] == client_email("123")
    assert payload_1["subId"] == SUBSCRIPTION_ID
    assert payload_1["tgId"] == 123
    assert payload_1["flow"] == "xtls-rprx-vision"  # vless + tcp
    assert ids_1 == [1]
    # Node 2: vmess gets no flow
    assert len(clients[2].add_calls) == 1
    payload_2, ids_2 = clients[2].add_calls[0]
    assert payload_2["email"] == client_email("123")
    assert payload_2["flow"] == ""  # vmess → no flow
    assert ids_2 == [2]
    saved = json.loads((tmp_path / "data.json").read_text(encoding="utf-8"))
    assert saved["clients"] == [
        {
            "id": "123",
            "comment": "Kirill (@resetand)",
            "subId": SUBSCRIPTION_ID,
        }
    ]


@pytest.mark.asyncio
async def test_returning_client_skips_all_nodes_when_all_inbounds_present(tmp_path: Path) -> None:
    store = prepare_store(tmp_path, clients=[{"id": "123", "comment": "Existing"}])
    email = client_email("123")
    initial = {
        1: {email: XuiClientInfo(client={"email": email}, inbound_ids=[1])},
        2: {email: XuiClientInfo(client={"email": email}, inbound_ids=[2])},
    }
    service, clients = service_with_fakes(
        store,
        {1: [inbound(1, "vless")], 2: [inbound(2, "trojan")]},
        initial,
    )

    result = await service.ensure_client("123", comment="New comment")

    assert result.created == 0
    assert result.reused == 2
    assert clients[1].add_calls == []
    assert clients[2].add_calls == []
    assert clients[1].attach_calls == []
    assert clients[2].attach_calls == []
    saved = json.loads((tmp_path / "data.json").read_text(encoding="utf-8"))
    assert saved["clients"] == [
        {
            "id": "123",
            "comment": "Existing",
            "subId": SUBSCRIPTION_ID,
            "legacySubId": "123",
        }
    ]


@pytest.mark.asyncio
async def test_partial_provisioning_adds_only_missing_cross_node_inbounds(tmp_path: Path) -> None:
    # Node 1 already has the client; node 2 does not.
    store = prepare_store(tmp_path)
    email = client_email("123")
    initial = {1: {email: XuiClientInfo(client={"email": email}, inbound_ids=[1])}}
    service, clients = service_with_fakes(
        store,
        {1: [inbound(1, "vless")], 2: [inbound(2, "trojan")]},
        initial,
    )

    result = await service.ensure_client("123", comment="Partial")

    assert result.created == 1
    assert result.reused == 1
    assert clients[1].add_calls == []
    assert len(clients[2].add_calls) == 1
    assert clients[2].add_calls[0][0]["email"] == email
    assert clients[2].add_calls[0][0]["subId"] == SUBSCRIPTION_ID


@pytest.mark.asyncio
async def test_partial_provisioning_attaches_missing_inbounds_within_a_node(tmp_path: Path) -> None:
    # One node with two target inbounds; client exists but only on inbound 1.
    store = prepare_store(
        tmp_path,
        inbounds=[
            {"tag": "a", "label": "A", "nodeId": 1, "xuiInboundId": 1},
            {"tag": "b", "label": "B", "nodeId": 1, "xuiInboundId": 2},
        ],
    )
    email = client_email("123")
    initial = {1: {email: XuiClientInfo(client={"email": email}, inbound_ids=[1])}}
    service, clients = service_with_fakes(
        store,
        {1: [inbound(1, "vless"), inbound(2, "vless")]},
        initial,
    )

    result = await service.ensure_client("123", comment="Attach missing")

    assert result.created == 1
    assert result.reused == 1
    assert clients[1].add_calls == []
    assert len(clients[1].attach_calls) == 1
    assert clients[1].attach_calls[0] == (email, [2])


@pytest.mark.asyncio
async def test_external_inbounds_are_skipped_during_provisioning(tmp_path: Path) -> None:
    store = prepare_store(
        tmp_path,
        inbounds=[
            {"label": "External", "uri": "vless://external#External"},
            {"label": "One", "nodeId": 1, "xuiInboundId": 1},
        ],
    )
    service, clients = service_with_fakes(store, {1: [inbound(1, "vless")], 2: []})

    await service.ensure_client("123", comment="Skip external")

    assert len(clients[1].add_calls) == 1
    assert 2 not in clients


@pytest.mark.asyncio
async def test_explicit_client_inbound_tags_restrict_provisioning_scope(tmp_path: Path) -> None:
    store = prepare_store(
        tmp_path,
        clients=[{"id": "123", "comment": "Existing", "inboundTags": ["personal"]}],
        inbounds=[
            {"tag": "default", "label": "Default", "nodeId": 1, "xuiInboundId": 1},
            {"tag": "personal", "label": "Personal", "nodeId": 2, "xuiInboundId": 2},
        ],
    )
    service, clients = service_with_fakes(store, {1: [inbound(1, "vless")], 2: [inbound(2, "vless")]})

    result = await service.ensure_client("123", comment="Keep override")

    assert result.created == 1
    assert 1 not in clients  # node 1 not touched
    assert clients[2].add_calls[0][0]["email"] == client_email("123")
    assert result.client.inbound_tags == ["personal"]


@pytest.mark.asyncio
async def test_vless_flow_applied_when_all_node_targets_are_tcp_vless(tmp_path: Path) -> None:
    store = prepare_store(tmp_path, inbounds=[{"label": "One", "nodeId": 1, "xuiInboundId": 1}])
    service = ProvisioningService(store, default_vless_flow="custom-flow")

    # TCP vless → flow set
    tcp_flow = service._compute_node_flow([inbound(1, "vless", network="tcp")])
    assert tcp_flow == "custom-flow"


@pytest.mark.asyncio
async def test_vless_flow_empty_for_non_tcp_or_non_vless_inbounds(tmp_path: Path) -> None:
    store = prepare_store(tmp_path, inbounds=[{"label": "One", "nodeId": 1, "xuiInboundId": 1}])
    service = ProvisioningService(store, default_vless_flow="custom-flow")

    ws_flow = service._compute_node_flow([inbound(1, "vless", network="ws")])
    vmess_flow = service._compute_node_flow([inbound(1, "vmess")])
    assert ws_flow == ""
    assert vmess_flow == ""


@pytest.mark.asyncio
async def test_vless_flow_empty_when_mixed_tcp_and_non_tcp_vless_on_same_node(tmp_path: Path) -> None:
    store = prepare_store(tmp_path)
    service = ProvisioningService(store, default_vless_flow="custom-flow")

    mixed_flow = service._compute_node_flow([
        inbound(1, "vless", network="tcp"),
        inbound(2, "vless", network="ws"),
    ])
    assert mixed_flow == ""


@pytest.mark.asyncio
async def test_client_payload_contains_required_fields_without_credentials(tmp_path: Path) -> None:
    store = prepare_store(tmp_path)
    service = ProvisioningService(store)

    payload = service._build_client_payload(
        client_id="456",
        sub_id="sub-xyz",
        comment="Test user",
        telegram_id=456,
        flow="xtls-rprx-vision",
    )

    assert payload["email"] == client_email("456")
    assert payload["subId"] == "sub-xyz"
    assert payload["comment"] == "Test user"
    assert payload["tgId"] == 456
    assert payload["flow"] == "xtls-rprx-vision"
    assert payload["enable"] is True
    # Server generates credentials — no id/password/auth in payload
    assert "id" not in payload
    assert "password" not in payload
    assert "auth" not in payload


@pytest.mark.asyncio
async def test_concurrent_provisioning_for_same_client_is_serialized(tmp_path: Path) -> None:
    store = prepare_store(tmp_path, inbounds=[{"label": "One", "nodeId": 1, "xuiInboundId": 1}])
    service, clients = service_with_fakes(store, {1: [inbound(1, "vless")]})

    await asyncio.gather(
        service.ensure_client("123", comment="Concurrent"),
        service.ensure_client("123", comment="Concurrent"),
    )

    assert len(clients[1].add_calls) == 1
