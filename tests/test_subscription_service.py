from __future__ import annotations

import base64
import json
import tarfile
from collections.abc import Mapping
from io import BytesIO
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vpn_control_plane.app import create_app
from vpn_control_plane.config import Settings, build_public_subscription_base_url, normalize_subscription_route
from vpn_control_plane.data import ControlPlaneStore, NodeRecord
from vpn_control_plane.http.routes import create_router
from vpn_control_plane.provisioning import client_email
from vpn_control_plane.subscription import (
    SubscriptionService,
    UnknownSubscriptionClientError,
    build_public_subscription_url,
    render_subscription_response,
)
from vpn_control_plane.xui import XuiInbound

JsonObject = dict[str, Any]
HAPP_ROUTING_RULES = "happ://routing/onadd/eyJOYW1lIjoiUlUgRGlyZWN0In0="


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def prepare_store(
    tmp_path: Path,
    *,
    clients: list[JsonObject] | None = None,
    nodes: list[JsonObject] | None = None,
    inbounds: list[JsonObject] | None = None,
    subscription: JsonObject | None = None,
) -> ControlPlaneStore:
    write_json(
        tmp_path / "data.json",
        build_state(
            clients=clients or [{"id": "123", "comment": "Existing"}],
            nodes=nodes,
            inbounds=inbounds,
            subscription=subscription or {},
        ),
    )
    return ControlPlaneStore(tmp_path / "data.json")


def build_state(
    *,
    clients: list[JsonObject],
    nodes: list[JsonObject] | None = None,
    inbounds: list[JsonObject] | None = None,
    subscription: JsonObject,
) -> JsonObject:
    raw_nodes = nodes or [
        {"id": 1, "host": "node-1.example.test", "port": 443, "basePath": "/panel/", "apiToken": "token-1"},
        {"id": 2, "host": "node-2.example.test", "port": 443, "basePath": "/panel/", "apiToken": "token-2"},
    ]
    raw_inbounds = inbounds or [
        {"label": "One", "nodeId": 1, "xuiInboundId": 1},
        {"label": "External", "uri": "vless://external#External"},
        {"label": "Two", "nodeId": 1, "xuiInboundId": 2},
    ]
    node_inbounds_by_id: dict[int, list[JsonObject]] = {int(node["id"]): [] for node in raw_nodes}
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

    state_nodes = []
    for node in raw_nodes:
        state_node = dict(node)
        state_node["inbounds"] = node.get("inbounds", node_inbounds_by_id[int(node["id"])])
        state_nodes.append(state_node)
    return {
        "nodes": state_nodes,
        "externalInbounds": external_inbounds,
        "clients": clients,
        "defaultClientInboundTags": default_tags,
        "subscription": subscription,
    }


class FakeXuiClient:
    def __init__(
        self,
        node: NodeRecord,
        inbounds_by_key: Mapping[tuple[int, int], XuiInbound | Exception | None],
    ) -> None:
        self.node = node
        self.inbounds_by_key = inbounds_by_key
        self.closed = False

    async def get_inbound(self, inbound_id: int) -> XuiInbound | None:
        value = self.inbounds_by_key.get((self.node.id, inbound_id))
        if isinstance(value, Exception):
            raise value
        return value

    async def list_inbounds(self) -> list[XuiInbound]:
        inbounds: list[XuiInbound] = []
        for (node_id, _inbound_id), value in self.inbounds_by_key.items():
            if node_id != self.node.id:
                continue
            if isinstance(value, Exception):
                raise value
            if value is not None:
                inbounds.append(value)
        return inbounds

    async def close(self) -> None:
        self.closed = True


def service_with_fakes(
    store: ControlPlaneStore,
    inbounds_by_key: Mapping[tuple[int, int], XuiInbound | Exception | None],
) -> SubscriptionService:
    def factory(node: NodeRecord) -> FakeXuiClient:
        return FakeXuiClient(node, inbounds_by_key)

    return SubscriptionService(
        store,
        public_base_url="https://resetand.my.id:2096/sub/",
        node_client_factory=cast(Any, factory),
    )


def xui_inbound(
    inbound_id: int,
    *,
    protocol: str = "vless",
    clients: list[JsonObject] | None = None,
    client_stats: list[JsonObject] | None = None,
    stream_settings: JsonObject | None = None,
    settings: JsonObject | None = None,
    port: int = 443,
    enable: bool = True,
) -> XuiInbound:
    parsed_settings = {"clients": clients if clients is not None else [vless_client()]}
    parsed_settings.update(settings or {})
    parsed_stream_settings = stream_settings or {"network": "tcp", "security": "none"}
    raw = {
        "id": inbound_id,
        "protocol": protocol,
        "listen": "",
        "port": port,
        "enable": enable,
        "settings": parsed_settings,
        "streamSettings": parsed_stream_settings,
        "sniffing": {},
    }
    if client_stats is not None:
        raw["clientStats"] = client_stats
    return XuiInbound(
        id=inbound_id,
        protocol=protocol,
        settings=parsed_settings,
        stream_settings=parsed_stream_settings,
        sniffing={},
        raw=raw,
    )


def vless_client(
    *,
    sub_id: str = "123",
    email: str = client_email("123"),
    client_id: str = "client-uuid",
) -> JsonObject:
    return {"id": client_id, "email": email, "subId": sub_id, "flow": ""}


def default_node_inbounds() -> dict[tuple[int, int], XuiInbound]:
    return {
        (1, 1): xui_inbound(1, clients=[vless_client(client_id="node-one")]),
        (1, 2): xui_inbound(
            2,
            protocol="trojan",
            clients=[{"password": "node-two", "email": client_email("123"), "subId": "123"}],
        ),
    }


def test_builds_legacy_public_subscription_url() -> None:
    assert build_public_subscription_url("https://resetand.my.id:2096/sub/", "123456789") == (
        "https://resetand.my.id:2096/sub/123456789"
    )
    assert (
        build_public_subscription_url("https://example.test/sub", "client 1") == "https://example.test/sub/client%201"
    )


def test_subscription_endpoint_settings_normalize_route_and_derive_public_base_url() -> None:
    settings = Settings.model_validate(
        {
            "VPN_SUBSCRIPTION_ROUTE": "sub",
            "VPN_SUBSCRIPTION_DOMAIN": "resetand.my.id",
            "VPN_SUBSCRIPTION_PORT": "2096",
            "VPN_TELEGRAM_BOT_TOKEN": "token",
            "VPN_TELEGRAM_ADMIN_IDS": "1",
        }
    )

    assert normalize_subscription_route("/sub") == "/sub/"
    assert settings.subscription_route == "/sub/"
    assert settings.public_subscription_base_url == "https://resetand.my.id:2096/sub"
    assert build_public_subscription_base_url("resetand.my.id", 443, "/sub/") == "https://resetand.my.id/sub"


@pytest.mark.asyncio
async def test_unknown_subscription_client_is_rejected(tmp_path: Path) -> None:
    service = service_with_fakes(prepare_store(tmp_path, clients=[]), {})

    with pytest.raises(UnknownSubscriptionClientError):
        await service.build("missing")


@pytest.mark.asyncio
async def test_builds_node_and_external_links_in_inbounds_file_order(tmp_path: Path) -> None:
    service = service_with_fakes(
        prepare_store(tmp_path),
        {
            (1, 1): xui_inbound(1, clients=[vless_client(client_id="node-one")]),
            (1, 2): xui_inbound(
                2,
                protocol="trojan",
                clients=[{"password": "node-two", "email": client_email("123"), "subId": "123"}],
            ),
        },
    )

    subscription = await service.build("123")

    assert subscription.links == [
        "vless://node-one@node-1.example.test:443?type=tcp&encryption=none&security=none#One",
        "vless://external#External",
        "trojan://node-two@node-1.example.test:443?type=tcp&security=none#Two",
    ]


@pytest.mark.asyncio
async def test_disabled_node_inbound_is_ignored_without_node_error(tmp_path: Path) -> None:
    service = service_with_fakes(
        prepare_store(tmp_path),
        {
            (1, 1): xui_inbound(1, clients=[vless_client(client_id="node-one")]),
            (1, 2): xui_inbound(
                2,
                protocol="trojan",
                clients=[{"password": "node-two", "email": client_email("123"), "subId": "123"}],
                enable=False,
            ),
        },
    )

    subscription = await service.build("123")

    assert subscription.links == [
        "vless://node-one@node-1.example.test:443?type=tcp&encryption=none&security=none#One",
        "vless://external#External",
    ]
    assert subscription.node_errors == ()


@pytest.mark.asyncio
async def test_build_adds_inbound_label_fragment_when_external_link_has_no_name(tmp_path: Path) -> None:
    service = service_with_fakes(
        prepare_store(
            tmp_path,
            inbounds=[{"label": "🇩🇪 Германия ⭐", "uri": "vless://external?type=tcp"}],
        ),
        {},
    )

    subscription = await service.build("123")

    assert subscription.links == [
        "vless://external?type=tcp#%F0%9F%87%A9%F0%9F%87%AA%20%D0%93%D0%B5%D1%80%D0%BC%D0%B0%D0%BD%D0%B8%D1%8F%20%E2%AD%90"
    ]


@pytest.mark.asyncio
async def test_sub_id_resolves_client_and_fetches_by_xui_fallback_email(tmp_path: Path) -> None:
    service = SubscriptionService(
        prepare_store(
            tmp_path,
            clients=[{"id": "123", "comment": "Migrated", "subId": "personal-token", "legacySubId": "123"}],
            inbounds=[{"label": "One", "nodeId": 1, "xuiInboundId": 1}],
        ),
        public_base_url="https://resetand.my.id:2096/sub",
        node_client_factory=cast(
            Any,
            lambda node: FakeXuiClient(
                node,
                {(1, 1): xui_inbound(1, clients=[vless_client(sub_id="legacy-sub", client_id="legacy-uuid")])},
            ),
        ),
    )

    subscription = await service.build("personal-token")

    assert subscription.links == [
        "vless://legacy-uuid@node-1.example.test:443?type=tcp&encryption=none&security=none#One"
    ]
    assert subscription.public_url == "https://resetand.my.id:2096/sub/personal-token"


@pytest.mark.asyncio
async def test_client_id_request_resolves_client_with_separate_effective_sub_id(tmp_path: Path) -> None:
    service = SubscriptionService(
        prepare_store(
            tmp_path,
            clients=[{"id": "123", "comment": "Migrated", "subId": "personal-token", "legacySubId": "123"}],
            inbounds=[{"label": "One", "nodeId": 1, "xuiInboundId": 1}],
        ),
        public_base_url="https://resetand.my.id:2096/sub",
        node_client_factory=cast(
            Any,
            lambda node: FakeXuiClient(
                node,
                {(1, 1): xui_inbound(1, clients=[vless_client(sub_id="legacy-sub", client_id="legacy-uuid")])},
            ),
        ),
    )

    subscription = await service.build("123")

    assert subscription.links == [
        "vless://legacy-uuid@node-1.example.test:443?type=tcp&encryption=none&security=none#One"
    ]
    assert subscription.public_url == "https://resetand.my.id:2096/sub/personal-token"


@pytest.mark.asyncio
async def test_sub_id_is_canonical_and_legacy_sub_id_remains_allowed(tmp_path: Path) -> None:
    service = SubscriptionService(
        prepare_store(
            tmp_path,
            clients=[
                {
                    "id": "123",
                    "comment": "Migrated",
                    "subId": "personal-token",
                    "legacySubId": "123",
                }
            ],
            inbounds=[{"label": "One", "nodeId": 1, "xuiInboundId": 1}],
        ),
        public_base_url="https://resetand.my.id:2096/sub",
        node_client_factory=cast(
            Any,
            lambda node: FakeXuiClient(
                node,
                {(1, 1): xui_inbound(1, clients=[vless_client(sub_id="legacy-sub", client_id="legacy-uuid")])},
            ),
        ),
    )

    subscription = await service.build("123")

    assert subscription.links == [
        "vless://legacy-uuid@node-1.example.test:443?type=tcp&encryption=none&security=none#One"
    ]
    assert subscription.public_url == "https://resetand.my.id:2096/sub/personal-token"


@pytest.mark.asyncio
async def test_sub_id_disables_legacy_ids_when_legacy_sub_id_is_absent(tmp_path: Path) -> None:
    service = service_with_fakes(
        prepare_store(tmp_path, clients=[{"id": "123", "comment": "New", "subId": "personal-token"}]),
        default_node_inbounds(),
    )

    with pytest.raises(UnknownSubscriptionClientError):
        await service.build("123")


@pytest.mark.asyncio
async def test_partial_node_failure_keeps_available_links(tmp_path: Path) -> None:
    service = service_with_fakes(
        prepare_store(
            tmp_path,
            inbounds=[
                {"label": "One", "nodeId": 1, "xuiInboundId": 1},
                {"label": "External", "uri": "vless://external#External"},
                {"label": "Two", "nodeId": 2, "xuiInboundId": 2},
            ],
        ),
        {
            (1, 1): RuntimeError("node is down"),
            (2, 2): xui_inbound(
                2,
                protocol="trojan",
                clients=[{"password": "node-two", "email": client_email("123"), "subId": "123"}],
            ),
        },
    )

    subscription = await service.build("123")

    assert subscription.links == [
        "vless://external#External",
        "trojan://node-two@node-2.example.test:443?type=tcp&security=none#Two",
    ]
    assert subscription.node_errors


@pytest.mark.asyncio
async def test_missing_node_client_is_ignored_without_breaking_external_links(tmp_path: Path) -> None:
    service = service_with_fakes(
        prepare_store(
            tmp_path,
            inbounds=[
                {"label": "One", "nodeId": 1, "xuiInboundId": 1},
                {"label": "External", "uri": "vless://external#External"},
            ],
        ),
        {(1, 1): xui_inbound(1, clients=[vless_client(sub_id="other", email=client_email("other"))])},
    )

    subscription = await service.build("123")

    assert subscription.links == ["vless://external#External"]
    assert subscription.node_errors == ()


@pytest.mark.asyncio
async def test_missing_node_client_falls_back_to_node_xui_fallback_client(tmp_path: Path) -> None:
    service = service_with_fakes(
        prepare_store(
            tmp_path,
            nodes=[
                {
                    "id": 1,
                    "host": "node-1.example.test",
                    "port": 443,
                    "basePath": "/panel/",
                    "apiToken": "token-1",
                    "xuiFallbackClientEmail": "default@example.test",
                    "inbounds": [{"tag": "node-one", "label": "One", "xuiInboundId": 1}],
                }
            ],
            inbounds=[{"tag": "node-one", "label": "One", "nodeId": 1, "xuiInboundId": 1}],
        ),
        {
            (1, 1): xui_inbound(
                1,
                clients=[
                    vless_client(sub_id="other", email=client_email("other"), client_id="other-uuid"),
                    vless_client(sub_id="default", email="default@example.test", client_id="default-uuid"),
                ],
            )
        },
    )

    subscription = await service.build("123")

    assert subscription.links == [
        "vless://default-uuid@node-1.example.test:443?type=tcp&encryption=none&security=none#One"
    ]
    assert subscription.node_errors == ()


@pytest.mark.asyncio
async def test_inbound_xui_fallback_client_overrides_node_fallback_client(tmp_path: Path) -> None:
    service = service_with_fakes(
        prepare_store(
            tmp_path,
            nodes=[
                {
                    "id": 1,
                    "host": "node-1.example.test",
                    "port": 443,
                    "basePath": "/panel/",
                    "apiToken": "token-1",
                    "xuiFallbackClientEmail": "node-default@example.test",
                    "inbounds": [
                        {
                            "tag": "node-one",
                            "label": "One",
                            "xuiInboundId": 1,
                            "xuiFallbackClientEmail": "inbound-default@example.test",
                        }
                    ],
                }
            ],
            inbounds=[{"tag": "node-one", "label": "One", "nodeId": 1, "xuiInboundId": 1}],
        ),
        {
            (1, 1): xui_inbound(
                1,
                clients=[
                    vless_client(sub_id="node-default", email="node-default@example.test", client_id="node-default"),
                    vless_client(
                        sub_id="inbound-default",
                        email="inbound-default@example.test",
                        client_id="inbound-default",
                    ),
                ],
            )
        },
    )

    subscription = await service.build("123")

    assert subscription.links == [
        "vless://inbound-default@node-1.example.test:443?type=tcp&encryption=none&security=none#One"
    ]
    assert subscription.node_errors == ()


@pytest.mark.asyncio
async def test_xui_fallback_client_uses_only_default_client_inbound_tags(tmp_path: Path) -> None:
    service = service_with_fakes(
        prepare_store(
            tmp_path,
            nodes=[
                {
                    "id": 1,
                    "host": "node-1.example.test",
                    "port": 443,
                    "basePath": "/panel/",
                    "apiToken": "token-1",
                    "xuiFallbackClientEmail": "default@example.test",
                    "inbounds": [
                        {"tag": "allowed", "label": "Allowed", "xuiInboundId": 1},
                        {"tag": "blocked", "label": "Blocked", "xuiInboundId": 2},
                    ],
                }
            ],
            inbounds=[{"tag": "allowed", "label": "Allowed", "nodeId": 1, "xuiInboundId": 1}],
        ),
        {
            (1, 1): xui_inbound(
                1,
                clients=[vless_client(sub_id="default", email="default@example.test", client_id="allowed-uuid")],
            ),
            (1, 2): xui_inbound(
                2,
                clients=[vless_client(sub_id="default", email="default@example.test", client_id="blocked-uuid")],
            ),
        },
    )

    subscription = await service.build("123")

    assert subscription.links == [
        "vless://allowed-uuid@node-1.example.test:443?type=tcp&encryption=none&security=none#Allowed"
    ]


@pytest.mark.asyncio
async def test_xui_fallback_client_uses_only_client_inbound_tags(tmp_path: Path) -> None:
    service = service_with_fakes(
        prepare_store(
            tmp_path,
            clients=[{"id": "123", "comment": "Existing", "inboundTags": ["allowed"]}],
            nodes=[
                {
                    "id": 1,
                    "host": "node-1.example.test",
                    "port": 443,
                    "basePath": "/panel/",
                    "apiToken": "token-1",
                    "xuiFallbackClientEmail": "default@example.test",
                    "inbounds": [
                        {"tag": "allowed", "label": "Allowed", "xuiInboundId": 1},
                        {"tag": "blocked", "label": "Blocked", "xuiInboundId": 2},
                    ],
                }
            ],
            inbounds=[
                {"tag": "allowed", "label": "Allowed", "nodeId": 1, "xuiInboundId": 1},
                {"tag": "blocked", "label": "Blocked", "nodeId": 1, "xuiInboundId": 2},
            ],
        ),
        {
            (1, 1): xui_inbound(
                1,
                clients=[vless_client(sub_id="default", email="default@example.test", client_id="allowed-uuid")],
            ),
            (1, 2): xui_inbound(
                2,
                clients=[vless_client(sub_id="default", email="default@example.test", client_id="blocked-uuid")],
            ),
        },
    )

    subscription = await service.build("123")

    assert subscription.links == [
        "vless://allowed-uuid@node-1.example.test:443?type=tcp&encryption=none&security=none#Allowed"
    ]


@pytest.mark.asyncio
async def test_renders_base64_text_response_with_metadata_headers(tmp_path: Path) -> None:
    store = prepare_store(
        tmp_path,
        subscription={
            "profileTitle": "Family VPN",
            "profileUpdateInterval": 24,
            "subscriptionUserinfo": "upload=0; download=4460105213; total=2147483648",
            "supportUrl": "https://support.example.test",
            "announce": "Maintenance tonight",
            "routing": HAPP_ROUTING_RULES,
        },
    )
    service = service_with_fakes(store, default_node_inbounds())

    subscription = await service.build("123")
    response = render_subscription_response(subscription)

    decoded = base64.b64decode(response.body).decode("utf-8")
    assert decoded == (
        "vless://node-one@node-1.example.test:443?type=tcp&encryption=none&security=none#One\n"
        "vless://external#External\n"
        "trojan://node-two@node-1.example.test:443?type=tcp&security=none#Two\n"
    )
    assert response.media_type == "text/plain; charset=utf-8"
    assert response.headers["content-disposition"] == 'attachment; filename="subscription.txt"'
    assert response.headers["profile-title"] == "base64:RmFtaWx5IFZQTg=="
    assert response.headers["profile-update-interval"] == "24"
    assert response.headers["profile-web-page-url"] == "https://resetand.my.id:2096/sub/123"
    assert response.headers["subscription-userinfo"] == "upload=0; download=4460105213; total=2147483648"
    assert response.headers["support-url"] == "https://support.example.test"
    assert response.headers["announce"] == "base64:TWFpbnRlbmFuY2UgdG9uaWdodA=="
    assert response.headers["routing-enable"] == "true"
    assert response.headers["routing"] == HAPP_ROUTING_RULES


@pytest.mark.asyncio
async def test_renders_userinfo_with_aggregated_client_traffic(tmp_path: Path) -> None:
    store = prepare_store(
        tmp_path,
        inbounds=[
            {"label": "One", "nodeId": 1, "xuiInboundId": 1},
            {"label": "Two", "nodeId": 1, "xuiInboundId": 2},
        ],
        subscription={"subscriptionUserinfo": "upload=0; download=0; total=2147483648; expire=1710442799"},
    )
    service = service_with_fakes(
        store,
        {
            (1, 1): xui_inbound(
                1,
                clients=[vless_client(client_id="node-one")],
                client_stats=[
                    {
                        "email": client_email("123"),
                        "subId": "123",
                        "up": 100,
                        "down": 200,
                        "total": 0,
                        "expiryTime": 0,
                    },
                    {
                        "email": client_email("other"),
                        "subId": "other",
                        "up": 999,
                        "down": 999,
                        "total": 0,
                        "expiryTime": 0,
                    },
                ],
            ),
            (1, 2): xui_inbound(
                2,
                protocol="trojan",
                clients=[
                    {
                        "password": "node-two",
                        "email": client_email("123"),
                        "subId": "123",
                    }
                ],
                client_stats=[
                    {
                        "email": client_email("123"),
                        "subId": "123",
                        "up": 7,
                        "down": 30,
                        "total": 0,
                        "expiryTime": 0,
                    }
                ],
            ),
        },
    )

    subscription = await service.build("123")
    response = render_subscription_response(subscription)

    assert response.headers["subscription-userinfo"] == "upload=107; download=230; total=2147483648; expire=1710442799"


@pytest.mark.asyncio
async def test_build_uses_list_inbounds_data_for_links_and_traffic(tmp_path: Path) -> None:
    class ListOnlyXuiClient:
        def __init__(self, node: NodeRecord) -> None:
            self.node = node

        async def get_inbound(self, inbound_id: int) -> XuiInbound | None:
            raise AssertionError("subscription build should not call get_inbound")

        async def list_inbounds(self) -> list[XuiInbound]:
            return [
                xui_inbound(
                    1,
                    clients=[vless_client(client_id="node-one")],
                    client_stats=[
                        {
                            "email": client_email("123"),
                            "subId": "123",
                            "up": 10,
                            "down": 20,
                            "total": 0,
                            "expiryTime": 0,
                        }
                    ],
                )
            ]

        async def close(self) -> None:
            return None

    service = SubscriptionService(
        prepare_store(
            tmp_path,
            inbounds=[{"label": "One", "nodeId": 1, "xuiInboundId": 1}],
        ),
        public_base_url="https://resetand.my.id:2096/sub/",
        node_client_factory=cast(Any, ListOnlyXuiClient),
    )

    subscription = await service.build("123")
    response = render_subscription_response(subscription)

    assert subscription.links == ["vless://node-one@node-1.example.test:443?type=tcp&encryption=none&security=none#One"]
    assert response.headers["subscription-userinfo"] == "upload=10; download=20; total=0"


@pytest.mark.asyncio
async def test_omits_userinfo_when_traffic_metadata_is_not_configured(tmp_path: Path) -> None:
    service = service_with_fakes(prepare_store(tmp_path), default_node_inbounds())

    subscription = await service.build("123")
    response = render_subscription_response(subscription)

    assert "subscription-userinfo" not in response.headers


@pytest.mark.asyncio
async def test_routing_enable_can_disable_configured_happ_routing_rules(tmp_path: Path) -> None:
    service = service_with_fakes(
        prepare_store(tmp_path, subscription={"routingEnable": False, "routing": HAPP_ROUTING_RULES}),
        default_node_inbounds(),
    )

    subscription = await service.build("123")
    response = render_subscription_response(subscription)

    assert response.headers["routing-enable"] == "false"
    assert response.headers["routing"] == HAPP_ROUTING_RULES


def test_subscription_route_returns_404_for_unknown_client(tmp_path: Path) -> None:
    store = prepare_store(tmp_path, clients=[])
    settings = Settings.model_validate(
        {
            "VPN_DATA_FILE": str(tmp_path / "data.json"),
            "VPN_SUBSCRIPTION_ROUTE": "/legacy-sub/",
            "VPN_SUBSCRIPTION_DOMAIN": "example.test",
            "VPN_TELEGRAM_BOT_TOKEN": "token",
            "VPN_TELEGRAM_ADMIN_IDS": "1",
        }
    )
    app = FastAPI()
    app.include_router(create_router(settings, store))

    response = TestClient(app).get("/legacy-sub/missing")

    assert response.status_code == 404


def test_subscription_route_returns_html_for_browser_accept(tmp_path: Path) -> None:
    store = prepare_store(
        tmp_path,
        inbounds=[{"label": "Germany", "uri": "vless://external#Germany"}],
        subscription={"profileTitle": "Family VPN"},
    )
    settings = Settings.model_validate(
        {
            "VPN_DATA_FILE": str(tmp_path / "data.json"),
            "VPN_SUBSCRIPTION_ROUTE": "/sub/",
            "VPN_SUBSCRIPTION_DOMAIN": "example.test",
            "VPN_SUBSCRIPTION_PORT": "443",
            "VPN_TELEGRAM_BOT_TOKEN": "token",
            "VPN_TELEGRAM_ADMIN_IDS": "1",
        }
    )
    app = FastAPI()
    app.include_router(create_router(settings, store))

    response = TestClient(app).get("/sub/123", headers={"accept": "text/html"})

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert "<title>Family VPN</title>" in response.text
    assert "data:image/png;base64," in response.text
    assert "Family VPN" in response.text
    assert "Ссылка для подключения" in response.text
    assert "Рекомендуемые приложения" in response.text
    assert "Доступные ключи" in response.text
    assert "Germany" in response.text
    assert "Семейный VPN" not in response.text
    assert "Подписка:" not in response.text
    assert "Decoded data" not in response.text
    assert "publicUrl" not in response.text
    assert "Key QR code" not in response.text


def test_subscription_route_returns_json_for_json_accept(tmp_path: Path) -> None:
    store = prepare_store(
        tmp_path,
        inbounds=[{"label": "Germany", "uri": "vless://external#Germany"}],
        subscription={"profileTitle": "Family VPN"},
    )
    settings = Settings.model_validate(
        {
            "VPN_DATA_FILE": str(tmp_path / "data.json"),
            "VPN_SUBSCRIPTION_ROUTE": "/sub/",
            "VPN_SUBSCRIPTION_DOMAIN": "example.test",
            "VPN_SUBSCRIPTION_PORT": "443",
            "VPN_TELEGRAM_BOT_TOKEN": "token",
            "VPN_TELEGRAM_ADMIN_IDS": "1",
        }
    )
    app = FastAPI()
    app.include_router(create_router(settings, store))

    response = TestClient(app).get("/sub/123", headers={"accept": "application/json"})

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json; charset=utf-8"
    payload = response.json()
    assert payload["subscription"]["title"] == "Family VPN"
    assert payload["subscription"]["profile_title"] == "Family VPN"
    assert payload["subscription"]["client_title"] == "Existing"
    assert payload["subscription"]["public_url"] == "https://example.test/sub/123"
    assert base64.b64decode(payload["subscription"]["encoded"]).decode("utf-8") == "vless://external#Germany\n"
    assert payload["links"] == [{"name": "Germany", "protocol": "VLESS", "url": "vless://external#Germany"}]
    assert payload["recommended_clients"]["android"]["name"] == "Happ"


def test_subscription_route_redirects_legacy_id_to_subscription_id(tmp_path: Path) -> None:
    store = prepare_store(
        tmp_path,
        clients=[
            {
                "id": "123",
                "comment": "Existing",
                "subId": "personal-token",
                "legacySubId": "123",
            }
        ],
        inbounds=[{"label": "Germany", "uri": "vless://external#Germany"}],
    )
    settings = Settings.model_validate(
        {
            "VPN_DATA_FILE": str(tmp_path / "data.json"),
            "VPN_SUBSCRIPTION_ROUTE": "/sub/",
            "VPN_SUBSCRIPTION_DOMAIN": "example.test",
            "VPN_SUBSCRIPTION_PORT": "443",
            "VPN_TELEGRAM_BOT_TOKEN": "token",
            "VPN_TELEGRAM_ADMIN_IDS": "1",
        }
    )
    app = FastAPI()
    app.include_router(create_router(settings, store))

    response = TestClient(app).get("/sub/123", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "https://example.test/sub/personal-token"


def test_subscription_route_keeps_legacy_base64_for_wildcard_accept(tmp_path: Path) -> None:
    store = prepare_store(
        tmp_path,
        inbounds=[{"label": "Germany", "uri": "vless://external#Germany"}],
    )
    settings = Settings.model_validate(
        {
            "VPN_DATA_FILE": str(tmp_path / "data.json"),
            "VPN_SUBSCRIPTION_ROUTE": "/sub/",
            "VPN_SUBSCRIPTION_DOMAIN": "example.test",
            "VPN_SUBSCRIPTION_PORT": "443",
            "VPN_TELEGRAM_BOT_TOKEN": "token",
            "VPN_TELEGRAM_ADMIN_IDS": "1",
        }
    )
    app = FastAPI()
    app.include_router(create_router(settings, store))

    response = TestClient(app).get("/sub/123", headers={"accept": "*/*"})

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert base64.b64decode(response.text).decode("utf-8") == "vless://external#Germany\n"


def test_create_app_exposes_health_after_configuration_and_state_load(tmp_path: Path) -> None:
    prepare_store(tmp_path)
    settings = Settings.model_validate(
        {
            "VPN_DATA_FILE": str(tmp_path / "data.json"),
            "VPN_SUBSCRIPTION_DOMAIN": "example.test",
            "VPN_TELEGRAM_BOT_TOKEN": "token",
            "VPN_TELEGRAM_ADMIN_IDS": "1",
            "BACKUP_HTTP_TOKEN": None,
        }
    )

    response = TestClient(create_app(settings, start_telegram=False)).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_create_app_fails_before_serving_when_required_state_is_missing(tmp_path: Path) -> None:
    settings = Settings.model_validate(
        {
            "VPN_DATA_FILE": str(tmp_path / "data.json"),
            "VPN_SUBSCRIPTION_DOMAIN": "example.test",
            "VPN_TELEGRAM_BOT_TOKEN": "token",
            "VPN_TELEGRAM_ADMIN_IDS": "1",
            "BACKUP_HTTP_TOKEN": None,
        }
    )

    with pytest.raises(ValueError, match="required data file is missing"):
        create_app(settings, start_telegram=False)


def test_backup_endpoint_requires_token_and_returns_control_plane_json_archive(tmp_path: Path) -> None:
    prepare_store(tmp_path, subscription={"announce": "Maintenance"})
    write_json(tmp_path / "runtime-cache.json", {"ignored": True})
    settings = Settings.model_validate(
        {
            "VPN_DATA_FILE": str(tmp_path / "data.json"),
            "VPN_SUBSCRIPTION_DOMAIN": "example.test",
            "VPN_TELEGRAM_BOT_TOKEN": "token",
            "VPN_TELEGRAM_ADMIN_IDS": "1",
            "BACKUP_HTTP_TOKEN": "backup-secret",
        }
    )
    client = TestClient(create_app(settings, start_telegram=False))

    missing_token = client.get("/backup")
    invalid_token = client.get("/backup", headers={"authorization": "Bearer wrong"})
    response = client.get("/backup", headers={"authorization": "Bearer backup-secret"})

    assert missing_token.status_code == 401
    assert invalid_token.status_code == 401
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/gzip"
    assert response.headers["content-disposition"] == 'attachment; filename="vpn-control-plane-data.tar.gz"'
    with tarfile.open(fileobj=BytesIO(response.content), mode="r:gz") as archive:
        assert sorted(archive.getnames()) == ["data.json"]
        data_file = archive.extractfile("data.json")
        assert data_file is not None
        assert json.loads(data_file.read().decode("utf-8"))["subscription"] == {"announce": "Maintenance"}


def test_backup_endpoint_is_disabled_without_configured_token(tmp_path: Path) -> None:
    prepare_store(tmp_path)
    settings = Settings.model_validate(
        {
            "VPN_DATA_FILE": str(tmp_path / "data.json"),
            "VPN_SUBSCRIPTION_DOMAIN": "example.test",
            "VPN_TELEGRAM_BOT_TOKEN": "token",
            "VPN_TELEGRAM_ADMIN_IDS": "1",
            "BACKUP_HTTP_TOKEN": None,
        }
    )

    response = TestClient(create_app(settings, start_telegram=False)).get("/backup")

    assert response.status_code == 403
