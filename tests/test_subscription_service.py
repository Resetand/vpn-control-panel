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
    build_public_subscription_token,
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
    """Simulates the 3x-ui v3.2.0 panel client: returns links/traffic keyed by email."""

    def __init__(
        self,
        node: NodeRecord,
        links_by_email: Mapping[str, list[str] | Exception],
        traffic_by_email: Mapping[str, JsonObject | Exception] | None = None,
    ) -> None:
        self.node = node
        self._links_by_email = links_by_email
        self._traffic_by_email: Mapping[str, JsonObject | Exception] = traffic_by_email or {}
        self.closed = False

    async def list_inbounds(self) -> list[XuiInbound]:
        # The panel keys each client link by the inbound *remark*; the control plane
        # maps allowed inbounds -> remark via list_inbounds. In these tests the inbound
        # remark equals the control-plane label (which is also the link fragment).
        return [
            XuiInbound(
                id=ib.xui_inbound_id,
                protocol="",
                settings={},
                stream_settings={},
                sniffing={},
                raw={"remark": ib.label},
            )
            for ib in self.node.inbounds
        ]

    async def get_client_links(self, email: str) -> list[str]:
        value = self._links_by_email.get(email)
        if isinstance(value, Exception):
            raise value
        return list(value) if value else []

    async def get_client_traffic(self, email: str) -> JsonObject | None:
        value = self._traffic_by_email.get(email)
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, dict) else None

    async def close(self) -> None:
        self.closed = True


def service_with_fakes(
    store: ControlPlaneStore,
    links_by_key: Mapping[tuple[int, str], list[str] | Exception],
    traffic_by_key: Mapping[tuple[int, str], JsonObject | Exception] | None = None,
) -> SubscriptionService:
    def factory(node: NodeRecord) -> FakeXuiClient:
        node_links = {email: val for (nid, email), val in links_by_key.items() if nid == node.id}
        node_traffic = {email: val for (nid, email), val in (traffic_by_key or {}).items() if nid == node.id}
        return FakeXuiClient(node, node_links, node_traffic)

    return SubscriptionService(
        store,
        public_base_url="https://resetand.my.id:2096/sub/",
        node_client_factory=cast(Any, factory),
    )


# ---------------------------------------------------------------------------
# Link helper: shorthand for expected panel-provided links.
# The panel generates links with the remark from the 3x-ui inbound config.
# ---------------------------------------------------------------------------

NODE_1_LINK_ONE = "vless://uuid-one@node-1.example.test:443?type=tcp&security=reality#One"
NODE_1_LINK_TWO = "trojan://pwd-two@node-1.example.test:443?type=tcp#Two"
NODE_2_LINK = "trojan://pwd-two@node-2.example.test:443?type=tcp#Two"

DEFAULT_EMAIL = client_email("123")

DEFAULT_NODE_LINKS: dict[tuple[int, str], list[str]] = {
    (1, DEFAULT_EMAIL): [NODE_1_LINK_ONE, NODE_1_LINK_TWO],
}


def test_builds_legacy_public_subscription_url() -> None:
    assert build_public_subscription_url("https://resetand.my.id:2096/sub/", "123456789") == (
        "https://resetand.my.id:2096/sub/123456789"
    )
    assert (
        build_public_subscription_url("https://example.test/sub", "client 1") == "https://example.test/sub/client%201"
    )


def test_builds_stable_public_subscription_token_from_salt() -> None:
    assert build_public_subscription_token("personal-token", "global-salt") == (
        "Q3tSn0X7FkI3CK0P7hoNvk12hXMeu5zwcl3VZgnHmS1"
    )


def test_subscription_endpoint_settings_normalize_route_and_derive_public_base_url() -> None:
    settings = Settings.model_validate(
        {
            "VPN_SUBSCRIPTION_ROUTE": "s",
            "VPN_SUBSCRIPTION_LEGACY_ROUTES": "sub,/sub/9f3aKx7PqLm2Zr8/",
            "VPN_SUBSCRIPTION_DOMAIN": "resetand.my.id",
            "VPN_SUBSCRIPTION_PORT": "2096",
            "VPN_TELEGRAM_BOT_TOKEN": "token",
            "VPN_TELEGRAM_ADMIN_IDS": "1",
        }
    )

    assert normalize_subscription_route("/sub") == "/sub/"
    assert settings.subscription_route == "/s/"
    assert settings.subscription_legacy_routes == ["/sub/", "/sub/9f3aKx7PqLm2Zr8/"]
    assert settings.public_subscription_base_url == "https://resetand.my.id:2096/s"
    assert build_public_subscription_base_url("resetand.my.id", 443, "/s/") == "https://resetand.my.id/s"


@pytest.mark.asyncio
async def test_unknown_subscription_client_is_rejected(tmp_path: Path) -> None:
    service = service_with_fakes(prepare_store(tmp_path, clients=[]), {})

    with pytest.raises(UnknownSubscriptionClientError):
        await service.build("missing")


@pytest.mark.asyncio
async def test_builds_links_in_tag_order_interleaving_external_links(tmp_path: Path) -> None:
    # Each link is emitted at its own tag's position, so external links interleave between
    # this node's inbounds (tag order: One, External, Two).
    service = service_with_fakes(
        prepare_store(tmp_path),
        {(1, DEFAULT_EMAIL): [NODE_1_LINK_ONE, NODE_1_LINK_TWO]},
    )

    subscription = await service.build("123")

    assert subscription.links == [
        NODE_1_LINK_ONE,
        "vless://external#External",
        NODE_1_LINK_TWO,
    ]


@pytest.mark.asyncio
async def test_external_link_fragment_is_added_when_missing(tmp_path: Path) -> None:
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
async def test_external_link_fragment_is_kept_when_present(tmp_path: Path) -> None:
    service = service_with_fakes(
        prepare_store(tmp_path, inbounds=[{"label": "DE", "uri": "vless://external#Germany"}]),
        {},
    )

    subscription = await service.build("123")

    assert subscription.links == ["vless://external#Germany"]


@pytest.mark.asyncio
async def test_disabled_node_client_returns_no_links_and_no_error(tmp_path: Path) -> None:
    # The panel simply returns [] for a disabled client; no node error is reported.
    service = service_with_fakes(
        prepare_store(tmp_path),
        {(1, DEFAULT_EMAIL): []},  # panel returns no links (e.g. all inbounds disabled)
    )

    subscription = await service.build("123")

    assert subscription.links == ["vless://external#External"]
    assert subscription.node_errors == ()


@pytest.mark.asyncio
async def test_sub_id_resolves_client_and_fetches_links_by_canonical_email(tmp_path: Path) -> None:
    service = service_with_fakes(
        prepare_store(
            tmp_path,
            clients=[{"id": "123", "comment": "Migrated", "subId": "personal-token", "legacySubId": "123"}],
            inbounds=[{"label": "One", "nodeId": 1, "xuiInboundId": 1}],
        ),
        {(1, DEFAULT_EMAIL): [NODE_1_LINK_ONE]},
    )

    subscription = await service.build("personal-token")

    assert subscription.links == [NODE_1_LINK_ONE]
    assert subscription.public_url == "https://resetand.my.id:2096/sub/personal-token"


@pytest.mark.asyncio
async def test_hashed_subscription_token_resolves_client_and_becomes_public_url(tmp_path: Path) -> None:
    store = prepare_store(
        tmp_path,
        clients=[{"id": "123", "comment": "Migrated", "subId": "personal-token", "legacySubId": "123"}],
        inbounds=[{"label": "One", "nodeId": 1, "xuiInboundId": 1}],
    )
    service = SubscriptionService(
        store,
        public_base_url="https://resetand.my.id:2096/s/",
        token_salt="global-salt",
        node_client_factory=cast(Any, lambda node: FakeXuiClient(node, {DEFAULT_EMAIL: [NODE_1_LINK_ONE]})),
    )

    token = build_public_subscription_token("personal-token", "global-salt")
    subscription = await service.build(token)

    assert subscription.links == [NODE_1_LINK_ONE]
    assert subscription.public_url == f"https://resetand.my.id:2096/s/{token}"


@pytest.mark.asyncio
async def test_legacy_client_id_request_resolves_client_with_separate_effective_sub_id(tmp_path: Path) -> None:
    service = service_with_fakes(
        prepare_store(
            tmp_path,
            clients=[{"id": "123", "comment": "Migrated", "subId": "personal-token", "legacySubId": "123"}],
            inbounds=[{"label": "One", "nodeId": 1, "xuiInboundId": 1}],
        ),
        {(1, DEFAULT_EMAIL): [NODE_1_LINK_ONE]},
    )

    subscription = await service.build("123")

    assert subscription.links == [NODE_1_LINK_ONE]
    assert subscription.public_url == "https://resetand.my.id:2096/sub/personal-token"


@pytest.mark.asyncio
async def test_sub_id_is_canonical_and_legacy_sub_id_remains_allowed(tmp_path: Path) -> None:
    service = service_with_fakes(
        prepare_store(
            tmp_path,
            clients=[{"id": "123", "comment": "Migrated", "subId": "personal-token", "legacySubId": "123"}],
            inbounds=[{"label": "One", "nodeId": 1, "xuiInboundId": 1}],
        ),
        {(1, DEFAULT_EMAIL): [NODE_1_LINK_ONE]},
    )

    subscription = await service.build("123")

    assert subscription.links == [NODE_1_LINK_ONE]
    assert subscription.public_url == "https://resetand.my.id:2096/sub/personal-token"


@pytest.mark.asyncio
async def test_sub_id_disables_legacy_ids_when_legacy_sub_id_is_absent(tmp_path: Path) -> None:
    service = service_with_fakes(
        prepare_store(tmp_path, clients=[{"id": "123", "comment": "New", "subId": "personal-token"}]),
        {(1, DEFAULT_EMAIL): [NODE_1_LINK_ONE]},
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
            (1, DEFAULT_EMAIL): RuntimeError("node is down"),
            (2, DEFAULT_EMAIL): [NODE_2_LINK],
        },
    )

    subscription = await service.build("123")

    assert subscription.links == ["vless://external#External", NODE_2_LINK]
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
        {(1, DEFAULT_EMAIL): []},  # panel returns [] → client not on this node
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
            (1, DEFAULT_EMAIL): [],  # primary email → no links
            (1, "default@example.test"): [NODE_1_LINK_ONE],
        },
    )

    subscription = await service.build("123")

    assert subscription.links == [NODE_1_LINK_ONE]
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
            (1, DEFAULT_EMAIL): [],
            (1, "node-default@example.test"): ["vless://node-default@..."],
            (1, "inbound-default@example.test"): ["vless://inbound-default@node-1.example.test:443?type=tcp#One"],
        },
    )

    subscription = await service.build("123")

    assert subscription.links == ["vless://inbound-default@node-1.example.test:443?type=tcp#One"]
    assert subscription.node_errors == ()


@pytest.mark.asyncio
async def test_xui_fallback_client_uses_only_default_client_inbound_tags(tmp_path: Path) -> None:
    # Client 123 has no inboundTags override — uses defaultClientInboundTags (only "allowed").
    # Even though node has another inbound "blocked", it's not in defaultClientInboundTags.
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
            (1, DEFAULT_EMAIL): [],
            (1, "default@example.test"): ["vless://allowed-uuid@...#Allowed"],
        },
    )

    subscription = await service.build("123")

    assert subscription.links == ["vless://allowed-uuid@...#Allowed"]


@pytest.mark.asyncio
async def test_xui_fallback_client_uses_only_client_inbound_tags(tmp_path: Path) -> None:
    # Client has explicit inboundTags = ["allowed"]; "blocked" tag is present in state but skipped.
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
            (1, DEFAULT_EMAIL): [],
            (1, "default@example.test"): ["vless://allowed-uuid@...#Allowed"],
        },
    )

    subscription = await service.build("123")

    assert subscription.links == ["vless://allowed-uuid@...#Allowed"]


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
    service = service_with_fakes(store, DEFAULT_NODE_LINKS)

    subscription = await service.build("123")
    response = render_subscription_response(subscription)

    decoded = base64.b64decode(response.body).decode("utf-8")
    # Links are emitted in tag order: One, External, Two.
    assert decoded == (f"{NODE_1_LINK_ONE}\nvless://external#External\n{NODE_1_LINK_TWO}\n")
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
        ],
        subscription={"subscriptionUserinfo": "upload=0; download=0; total=2147483648; expire=1710442799"},
    )
    service = service_with_fakes(
        store,
        {(1, DEFAULT_EMAIL): [NODE_1_LINK_ONE]},
        traffic_by_key={
            (1, DEFAULT_EMAIL): {"up": 107, "down": 230, "total": 0, "expiryTime": 0},
        },
    )

    subscription = await service.build("123")
    response = render_subscription_response(subscription)

    assert response.headers["subscription-userinfo"] == "upload=107; download=230; total=2147483648; expire=1710442799"


@pytest.mark.asyncio
async def test_build_maps_panel_links_to_allowed_inbounds_by_remark(tmp_path: Path) -> None:
    """The panel returns all of a client's links keyed by inbound remark; build() uses
    list_inbounds to map allowed inbounds -> remark -> link, and get_client_traffic for usage."""

    class PanelClient:
        def __init__(self, node: NodeRecord) -> None:
            self.node = node

        async def list_inbounds(self) -> list[XuiInbound]:
            return [XuiInbound(id=1, protocol="", settings={}, stream_settings={}, sniffing={}, raw={"remark": "One"})]

        async def get_client_links(self, email: str) -> list[str]:
            return [NODE_1_LINK_ONE]

        async def get_client_traffic(self, email: str) -> JsonObject | None:
            return {"up": 10, "down": 20, "total": 0, "expiryTime": 0}

        async def close(self) -> None:
            return None

    service = SubscriptionService(
        prepare_store(
            tmp_path,
            inbounds=[{"label": "One", "nodeId": 1, "xuiInboundId": 1}],
        ),
        public_base_url="https://resetand.my.id:2096/sub/",
        node_client_factory=cast(Any, PanelClient),
    )

    subscription = await service.build("123")
    response = render_subscription_response(subscription)

    assert subscription.links == [NODE_1_LINK_ONE]
    assert response.headers["subscription-userinfo"] == "upload=10; download=20; total=0"


@pytest.mark.asyncio
async def test_omits_userinfo_when_traffic_metadata_is_not_configured(tmp_path: Path) -> None:
    service = service_with_fakes(prepare_store(tmp_path), DEFAULT_NODE_LINKS)

    subscription = await service.build("123")
    response = render_subscription_response(subscription)

    assert "subscription-userinfo" not in response.headers


@pytest.mark.asyncio
async def test_routing_enable_can_disable_configured_happ_routing_rules(tmp_path: Path) -> None:
    service = service_with_fakes(
        prepare_store(tmp_path, subscription={"routingEnable": False, "routing": HAPP_ROUTING_RULES}),
        DEFAULT_NODE_LINKS,
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
            "VPN_SUBSCRIPTION_ROUTE": "/s/",
            "VPN_SUBSCRIPTION_DOMAIN": "example.test",
            "VPN_SUBSCRIPTION_PORT": "443",
            "VPN_SUBSCRIPTION_TOKEN_SALT": "global-salt",
            "VPN_TELEGRAM_BOT_TOKEN": "token",
            "VPN_TELEGRAM_ADMIN_IDS": "1",
        }
    )
    app = FastAPI()
    app.include_router(create_router(settings, store))

    token = build_public_subscription_token("123", "global-salt")
    response = TestClient(app).get(f"/s/{token}", headers={"accept": "application/json"})

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json; charset=utf-8"
    payload = response.json()
    assert payload["subscription"]["title"] == "Family VPN"
    assert payload["subscription"]["profile_title"] == "Family VPN"
    assert payload["subscription"]["client_title"] == "Existing"
    assert payload["subscription"]["public_url"] == f"https://example.test/s/{token}"
    assert base64.b64decode(payload["subscription"]["encoded"]).decode("utf-8") == "vless://external#Germany\n"
    assert payload["links"] == [{"name": "Germany", "protocol": "VLESS", "url": "vless://external#Germany"}]
    assert payload["recommended_clients"]["android"]["name"] == "Happ"


def test_subscription_route_adds_new_url_header_for_legacy_url(tmp_path: Path) -> None:
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
            "VPN_SUBSCRIPTION_ROUTE": "/s/",
            "VPN_SUBSCRIPTION_LEGACY_ROUTES": "/sub/,/sub/9f3aKx7PqLm2Zr8/",
            "VPN_SUBSCRIPTION_DOMAIN": "example.test",
            "VPN_SUBSCRIPTION_PORT": "443",
            "VPN_SUBSCRIPTION_TOKEN_SALT": "global-salt",
            "VPN_TELEGRAM_BOT_TOKEN": "token",
            "VPN_TELEGRAM_ADMIN_IDS": "1",
        }
    )
    app = FastAPI()
    app.include_router(create_router(settings, store))

    response = TestClient(app).get("/sub/9f3aKx7PqLm2Zr8/123")
    token = build_public_subscription_token("personal-token", "global-salt")

    assert response.status_code == 200
    assert response.headers["new-url"] == f"https://example.test/s/{token}"
    assert base64.b64decode(response.text).decode("utf-8") == "vless://external#Germany\n"


def test_subscription_route_redirects_html_legacy_url_to_canonical_url(tmp_path: Path) -> None:
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
            "VPN_SUBSCRIPTION_ROUTE": "/s/",
            "VPN_SUBSCRIPTION_LEGACY_ROUTES": "/sub/,/sub/9f3aKx7PqLm2Zr8/",
            "VPN_SUBSCRIPTION_DOMAIN": "example.test",
            "VPN_SUBSCRIPTION_PORT": "443",
            "VPN_SUBSCRIPTION_TOKEN_SALT": "global-salt",
            "VPN_TELEGRAM_BOT_TOKEN": "token",
            "VPN_TELEGRAM_ADMIN_IDS": "1",
        }
    )
    app = FastAPI()
    app.include_router(create_router(settings, store))

    response = TestClient(app).get(
        "/sub/9f3aKx7PqLm2Zr8/123",
        headers={"accept": "text/html"},
        follow_redirects=False,
    )
    token = build_public_subscription_token("personal-token", "global-salt")

    assert response.status_code == 302
    assert response.headers["location"] == f"https://example.test/s/{token}"


def test_subscription_route_omits_new_url_header_for_canonical_url(tmp_path: Path) -> None:
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
            "VPN_SUBSCRIPTION_ROUTE": "/s/",
            "VPN_SUBSCRIPTION_LEGACY_ROUTES": "/sub/,/sub/9f3aKx7PqLm2Zr8/",
            "VPN_SUBSCRIPTION_DOMAIN": "example.test",
            "VPN_SUBSCRIPTION_PORT": "443",
            "VPN_SUBSCRIPTION_TOKEN_SALT": "global-salt",
            "VPN_TELEGRAM_BOT_TOKEN": "token",
            "VPN_TELEGRAM_ADMIN_IDS": "1",
        }
    )
    app = FastAPI()
    app.include_router(create_router(settings, store))

    token = build_public_subscription_token("personal-token", "global-salt")
    response = TestClient(app).get(f"/s/{token}")

    assert response.status_code == 200
    assert "new-url" not in response.headers


def test_subscription_route_does_not_treat_primary_sub_route_as_legacy(tmp_path: Path) -> None:
    store = prepare_store(
        tmp_path,
        inbounds=[{"label": "Germany", "uri": "vless://external#Germany"}],
    )
    settings = Settings.model_validate(
        {
            "VPN_DATA_FILE": str(tmp_path / "data.json"),
            "VPN_SUBSCRIPTION_ROUTE": "/sub/",
            "VPN_SUBSCRIPTION_LEGACY_ROUTES": "/sub/",
            "VPN_SUBSCRIPTION_DOMAIN": "example.test",
            "VPN_SUBSCRIPTION_PORT": "443",
            "VPN_TELEGRAM_BOT_TOKEN": "token",
            "VPN_TELEGRAM_ADMIN_IDS": "1",
        }
    )
    app = FastAPI()
    app.include_router(create_router(settings, store))

    response = TestClient(app).get("/sub/123", headers={"accept": "text/html"}, follow_redirects=False)

    assert response.status_code == 200
    assert "new-url" not in response.headers


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
