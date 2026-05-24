from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any, cast

import pytest

from vpn_control_plane.data import ControlPlaneStore, NodeRecord
from vpn_control_plane.provisioning import ProvisioningService, legacy_client_email, telegram_client_id
from vpn_control_plane.xui import XuiAddClientResult, XuiInbound

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


def inbound(inbound_id: int, protocol: str = "vless", clients: list[JsonObject] | None = None) -> XuiInbound:
    return XuiInbound(
        id=inbound_id,
        protocol=protocol,
        settings={"clients": clients or []},
        stream_settings={"network": "tcp"},
        sniffing={},
        raw={},
    )


class FakeXuiClient:
    def __init__(self, node: NodeRecord, inbounds_by_node: dict[int, list[XuiInbound]]) -> None:
        self.node = node
        self.inbounds_by_node = inbounds_by_node
        self.added: list[tuple[int, JsonObject]] = []
        self.closed = False

    async def list_inbounds(self) -> list[XuiInbound]:
        return self.inbounds_by_node[self.node.id]

    async def add_client(self, inbound_id: int, client_payload: JsonObject) -> XuiAddClientResult:
        self.added.append((inbound_id, client_payload))
        target = next(candidate for candidate in self.inbounds_by_node[self.node.id] if candidate.id == inbound_id)
        target.settings.setdefault("clients", []).append(client_payload)
        return XuiAddClientResult(created=True)

    async def close(self) -> None:
        self.closed = True


def service_with_fakes(
    store: ControlPlaneStore,
    inbounds_by_node: dict[int, list[XuiInbound]],
) -> tuple[ProvisioningService, dict[int, FakeXuiClient]]:
    clients: dict[int, FakeXuiClient] = {}

    def factory(node: NodeRecord) -> FakeXuiClient:
        client = clients.get(node.id)
        if client is None:
            client = FakeXuiClient(node, inbounds_by_node)
            clients[node.id] = client
        return client

    service = ProvisioningService(
        store,
        node_client_factory=cast(Any, factory),
        uuid_factory=lambda: uuid.UUID("11111111-1111-1111-1111-111111111111"),
        random_bytes=lambda size: b"x" * size,
        subscription_id_factory=lambda: SUBSCRIPTION_ID,
    )
    return service, clients


@pytest.mark.asyncio
async def test_selects_control_plane_ids_and_legacy_email() -> None:
    assert telegram_client_id(123456789) == "123456789"
    assert legacy_client_email(1, "123456789") == "1_123456789"


@pytest.mark.asyncio
async def test_new_client_provisioning_creates_all_node_inbounds_and_persists_record(tmp_path: Path) -> None:
    store = prepare_store(tmp_path)
    service, clients = service_with_fakes(store, {1: [inbound(1, "vless")], 2: [inbound(2, "vmess")]})

    result = await service.ensure_telegram_user(123, comment="Kirill", username="resetand")

    assert result.client.id == "123"
    assert result.client.effective_sub_id == SUBSCRIPTION_ID
    assert result.created == 2
    assert clients[1].added[0][1]["email"] == "1_123"
    assert clients[1].added[0][1]["subId"] == SUBSCRIPTION_ID
    assert clients[1].added[0][1]["tgId"] == 123
    assert clients[1].added[0][1]["flow"] == "xtls-rprx-vision"
    assert clients[2].added[0][1]["email"] == "2_123"
    saved = json.loads((tmp_path / "data.json").read_text(encoding="utf-8"))
    assert saved["clients"] == [
        {
            "id": "123",
            "comment": "Kirill (@resetand)",
            "subId": SUBSCRIPTION_ID,
        }
    ]


@pytest.mark.asyncio
async def test_returning_client_does_not_create_or_overwrite_existing_key_material(tmp_path: Path) -> None:
    store = prepare_store(tmp_path, clients=[{"id": "123", "comment": "Existing"}])
    existing_one = {"email": "1_123", "id": "keep-uuid", "subId": "legacy-sub", "flow": "xtls-rprx-vision"}
    existing_two = {"email": "2_123", "password": "keep-password", "subId": "legacy-sub"}
    service, clients = service_with_fakes(
        store,
        {1: [inbound(1, "vless", [existing_one])], 2: [inbound(2, "trojan", [existing_two])]},
    )

    result = await service.ensure_client("123", comment="New comment")

    assert result.created == 0
    assert result.reused == 2
    assert clients[1].added == []
    assert clients[2].added == []
    assert existing_one["id"] == "keep-uuid"
    assert existing_two["password"] == "keep-password"
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
async def test_partial_provisioning_creates_only_missing_inbounds_with_existing_sub_id(tmp_path: Path) -> None:
    store = prepare_store(tmp_path)
    service, clients = service_with_fakes(
        store,
        {
            1: [inbound(1, "vless", [{"email": "1_123", "id": "keep", "subId": "legacy-sub"}])],
            2: [inbound(2, "trojan")],
        },
    )

    result = await service.ensure_client("123", comment="Partial")

    assert result.created == 1
    assert clients[1].added == []
    assert clients[2].added[0][1]["email"] == "2_123"
    assert clients[2].added[0][1]["subId"] == SUBSCRIPTION_ID
    assert clients[2].added[0][1]["password"] == "11111111-1111-1111-1111-111111111111"


@pytest.mark.asyncio
async def test_external_inbounds_are_skipped_during_provisioning(tmp_path: Path) -> None:
    store = prepare_store(
        tmp_path,
        inbounds=[
            {"label": "External", "uri": "vless://external#External"},
            {"label": "One", "nodeId": 1, "xuiInboundId": 1},
        ],
    )
    service, clients = service_with_fakes(store, {1: [inbound(1, "vless")], 2: [inbound(2, "vless")]})

    await service.ensure_client("123", comment="Skip external")

    assert len(clients[1].added) == 1
    assert 2 not in clients


@pytest.mark.asyncio
async def test_existing_client_explicit_inbound_tags_define_provisioning_scope(tmp_path: Path) -> None:
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
    assert 1 not in clients
    assert clients[2].added[0][1]["email"] == "2_123"
    assert result.client.inbound_tags == ["personal"]


@pytest.mark.asyncio
async def test_payload_generation_supports_vmess_trojan_and_shadowsocks(tmp_path: Path) -> None:
    store = prepare_store(tmp_path)
    service, _clients = service_with_fakes(store, {1: [inbound(1)], 2: [inbound(2)]})

    vmess = service.build_client_payload(inbound(1, "vmess"), client_id="c", sub_id="s", comment="C")
    trojan = service.build_client_payload(inbound(2, "trojan"), client_id="c", sub_id="s", comment="C")
    shadowsocks = service.build_client_payload(inbound(3, "shadowsocks"), client_id="c", sub_id="s", comment="C")
    shadowsocks_2022 = service.build_client_payload(
        XuiInbound(4, "shadowsocks", {"method": "2022-blake3-aes-256-gcm"}, {}, {}, {}),
        client_id="c",
        sub_id="s",
        comment="C",
    )

    assert vmess["id"] == "11111111-1111-1111-1111-111111111111"
    assert trojan["password"] == "11111111-1111-1111-1111-111111111111"
    assert shadowsocks["password"] == "11111111-1111-1111-1111-111111111111"
    assert shadowsocks_2022["method"] == ""
    assert shadowsocks_2022["password"] == "eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHg="


@pytest.mark.asyncio
async def test_vless_flow_is_configurable_and_applies_only_to_tcp_vless(tmp_path: Path) -> None:
    store = prepare_store(tmp_path)
    service = ProvisioningService(store, default_vless_flow="custom-flow")

    vless_tcp = service.build_client_payload(inbound(1, "vless"), client_id="c", sub_id="s", comment="C")
    vless_ws = service.build_client_payload(
        XuiInbound(2, "vless", {"clients": []}, {"network": "ws"}, {}, {}),
        client_id="c",
        sub_id="s",
        comment="C",
    )
    vmess_tcp = service.build_client_payload(inbound(3, "vmess"), client_id="c", sub_id="s", comment="C")

    assert vless_tcp["flow"] == "custom-flow"
    assert vless_ws["flow"] == ""
    assert vmess_tcp["flow"] == ""


@pytest.mark.asyncio
async def test_concurrent_provisioning_for_same_client_is_serialized(tmp_path: Path) -> None:
    store = prepare_store(tmp_path, inbounds=[{"label": "One", "nodeId": 1, "xuiInboundId": 1}])
    service, clients = service_with_fakes(store, {1: [inbound(1, "vless")]})

    await asyncio.gather(
        service.ensure_client("123", comment="Concurrent"),
        service.ensure_client("123", comment="Concurrent"),
    )

    assert len(clients[1].added) == 1
