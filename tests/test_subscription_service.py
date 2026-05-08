from __future__ import annotations

import base64
import json
import tarfile
from io import BytesIO
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vpn_control_plane.app import create_app
from vpn_control_plane.config import Settings, build_public_subscription_base_url, normalize_subscription_route
from vpn_control_plane.data import JsonStateStore, NodeRecord
from vpn_control_plane.http.routes import create_router
from vpn_control_plane.subscription import (
    SubscriptionService,
    UnknownSubscriptionClientError,
    build_public_subscription_url,
    render_subscription_response,
)

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
) -> JsonStateStore:
    write_json(
        tmp_path / "nodes.json",
        nodes
        or [
            {
                "id": 1,
                "host": "node-1.example.test",
                "port": 443,
                "webBasePath": "/panel/",
                "username": "u",
                "password": "p",
                "subscriptionBaseUrl": "https://node-1.example.test/sub",
            },
            {
                "id": 2,
                "host": "node-2.example.test",
                "port": 443,
                "webBasePath": "/panel/",
                "username": "u",
                "password": "p",
                "subscriptionBaseUrl": "https://node-2.example.test/sub",
            },
        ],
    )
    write_json(tmp_path / "clients.json", clients or [{"id": "123", "comment": "Existing"}])
    write_json(
        tmp_path / "inbounds.json",
        inbounds
        or [
            {"type": "node-inbound", "label": "One", "nodeId": 1, "inboundId": 1},
            {"type": "external-inbound", "label": "External", "uri": "vless://external#External"},
            {"type": "node-inbound", "label": "Two", "nodeId": 1, "inboundId": 2},
        ],
    )
    write_json(tmp_path / "subscription.json", subscription or {})
    return JsonStateStore(tmp_path)


class FakeXuiClient:
    def __init__(self, node: NodeRecord, links_by_node: dict[int, list[str] | Exception]) -> None:
        self.node = node
        self.links_by_node = links_by_node
        self.closed = False

    async def fetch_subscription_links(self, sub_id: str) -> list[str]:
        value = self.links_by_node[self.node.id]
        if isinstance(value, Exception):
            raise value
        return value

    async def close(self) -> None:
        self.closed = True


def service_with_fakes(
    store: JsonStateStore,
    links_by_node: dict[int, list[str] | Exception],
) -> SubscriptionService:
    def factory(node: NodeRecord) -> FakeXuiClient:
        return FakeXuiClient(node, links_by_node)

    return SubscriptionService(
        store,
        public_base_url="https://resetand.my.id:2096/sub/",
        node_client_factory=cast(Any, factory),
    )


def test_builds_legacy_public_subscription_url() -> None:
    assert build_public_subscription_url("https://resetand.my.id:2096/sub/", "123456789") == (
        "https://resetand.my.id:2096/sub/123456789"
    )
    assert build_public_subscription_url("https://example.test/sub", "client 1") == "https://example.test/sub/client%201"


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
        {1: ["vless://node-one#One", "trojan://node-two#Two"]},
    )

    subscription = await service.build("123")

    assert subscription.links == ["vless://node-one#One", "vless://external#External", "trojan://node-two#Two"]


@pytest.mark.asyncio
async def test_effective_sub_id_resolves_legacy_client_and_fetches_with_legacy_id(tmp_path: Path) -> None:
    seen_sub_ids: list[str] = []

    class RecordingFakeXuiClient(FakeXuiClient):
        async def fetch_subscription_links(self, sub_id: str) -> list[str]:
            seen_sub_ids.append(sub_id)
            return await super().fetch_subscription_links(sub_id)

    def factory(node: NodeRecord) -> RecordingFakeXuiClient:
        return RecordingFakeXuiClient(node, {1: ["vless://node-one#One"]})

    service = SubscriptionService(
        prepare_store(
            tmp_path,
            clients=[{"id": "123", "comment": "Migrated", "subId": "legacy-sub"}],
            inbounds=[{"type": "node-inbound", "label": "One", "nodeId": 1, "inboundId": 1}],
        ),
        public_base_url="https://resetand.my.id:2096/sub",
        node_client_factory=cast(Any, factory),
    )

    subscription = await service.build("legacy-sub")

    assert seen_sub_ids == ["legacy-sub"]
    assert subscription.public_url == "https://resetand.my.id:2096/sub/legacy-sub"


@pytest.mark.asyncio
async def test_client_id_request_resolves_client_with_separate_effective_sub_id(tmp_path: Path) -> None:
    seen_sub_ids: list[str] = []

    class RecordingFakeXuiClient(FakeXuiClient):
        async def fetch_subscription_links(self, sub_id: str) -> list[str]:
            seen_sub_ids.append(sub_id)
            return await super().fetch_subscription_links(sub_id)

    def factory(node: NodeRecord) -> RecordingFakeXuiClient:
        return RecordingFakeXuiClient(node, {1: ["vless://node-one#One"]})

    service = SubscriptionService(
        prepare_store(
            tmp_path,
            clients=[{"id": "123", "comment": "Migrated", "subId": "legacy-sub"}],
            inbounds=[{"type": "node-inbound", "label": "One", "nodeId": 1, "inboundId": 1}],
        ),
        public_base_url="https://resetand.my.id:2096/sub",
        node_client_factory=cast(Any, factory),
    )

    subscription = await service.build("123")

    assert seen_sub_ids == ["legacy-sub"]
    assert subscription.public_url == "https://resetand.my.id:2096/sub/legacy-sub"


@pytest.mark.asyncio
async def test_partial_node_failure_keeps_available_links(tmp_path: Path) -> None:
    service = service_with_fakes(
        prepare_store(
            tmp_path,
            inbounds=[
                {"type": "node-inbound", "label": "One", "nodeId": 1, "inboundId": 1},
                {"type": "external-inbound", "label": "External", "uri": "vless://external#External"},
                {"type": "node-inbound", "label": "Two", "nodeId": 2, "inboundId": 2},
            ],
        ),
        {1: RuntimeError("node is down"), 2: ["trojan://node-two#Two"]},
    )

    subscription = await service.build("123")

    assert subscription.links == ["vless://external#External", "trojan://node-two#Two"]
    assert subscription.node_errors


@pytest.mark.asyncio
async def test_renders_base64_text_response_with_metadata_headers(tmp_path: Path) -> None:
    store = prepare_store(
        tmp_path,
        subscription={
            "profile-title": "Family VPN",
            "profile-update-interval": 24,
            "subscription-userinfo": "upload=0; download=4460105213; total=2147483648",
            "support-url": "https://support.example.test",
            "announce": "Maintenance tonight",
            "routing": HAPP_ROUTING_RULES,
        },
    )
    service = service_with_fakes(store, {1: ["vless://node-one#One", "trojan://node-two#Two"]})

    subscription = await service.build("123")
    response = render_subscription_response(subscription)

    decoded = base64.b64decode(response.body).decode("utf-8")
    assert decoded == "vless://node-one#One\nvless://external#External\ntrojan://node-two#Two\n"
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
async def test_omits_userinfo_when_traffic_metadata_is_not_configured(tmp_path: Path) -> None:
    service = service_with_fakes(prepare_store(tmp_path), {1: ["vless://node-one#One", "trojan://node-two#Two"]})

    subscription = await service.build("123")
    response = render_subscription_response(subscription)

    assert "subscription-userinfo" not in response.headers


@pytest.mark.asyncio
async def test_routing_enable_can_disable_configured_happ_routing_rules(tmp_path: Path) -> None:
    service = service_with_fakes(
        prepare_store(tmp_path, subscription={"routing-enable": False, "routing": HAPP_ROUTING_RULES}),
        {1: ["vless://node-one#One", "trojan://node-two#Two"]},
    )

    subscription = await service.build("123")
    response = render_subscription_response(subscription)

    assert response.headers["routing-enable"] == "false"
    assert response.headers["routing"] == HAPP_ROUTING_RULES


def test_subscription_route_returns_404_for_unknown_client(tmp_path: Path) -> None:
    store = prepare_store(tmp_path, clients=[])
    settings = Settings.model_validate(
        {
            "VPN_DATA_DIR": str(tmp_path),
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


def test_create_app_exposes_health_after_configuration_and_state_load(tmp_path: Path) -> None:
    prepare_store(tmp_path)
    settings = Settings.model_validate(
        {
            "VPN_DATA_DIR": str(tmp_path),
            "VPN_SUBSCRIPTION_DOMAIN": "example.test",
            "VPN_TELEGRAM_BOT_TOKEN": "token",
            "VPN_TELEGRAM_ADMIN_IDS": "1",
        }
    )

    response = TestClient(create_app(settings, start_telegram=False)).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_create_app_fails_before_serving_when_required_state_is_missing(tmp_path: Path) -> None:
    settings = Settings.model_validate(
        {
            "VPN_DATA_DIR": str(tmp_path),
            "VPN_SUBSCRIPTION_DOMAIN": "example.test",
            "VPN_TELEGRAM_BOT_TOKEN": "token",
            "VPN_TELEGRAM_ADMIN_IDS": "1",
        }
    )

    with pytest.raises(ValueError, match="required data file is missing"):
        create_app(settings, start_telegram=False)


def test_backup_endpoint_requires_token_and_returns_control_plane_json_archive(tmp_path: Path) -> None:
    prepare_store(tmp_path, subscription={"announce": "Maintenance"})
    write_json(tmp_path / "runtime-cache.json", {"ignored": True})
    settings = Settings.model_validate(
        {
            "VPN_DATA_DIR": str(tmp_path),
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
        assert sorted(archive.getnames()) == ["clients.json", "inbounds.json", "nodes.json", "subscription.json"]
        subscription_file = archive.extractfile("subscription.json")
        assert subscription_file is not None
        assert json.loads(subscription_file.read().decode("utf-8")) == {"announce": "Maintenance"}


def test_backup_endpoint_is_disabled_without_configured_token(tmp_path: Path) -> None:
    prepare_store(tmp_path)
    settings = Settings.model_validate(
        {
            "VPN_DATA_DIR": str(tmp_path),
            "VPN_SUBSCRIPTION_DOMAIN": "example.test",
            "VPN_TELEGRAM_BOT_TOKEN": "token",
            "VPN_TELEGRAM_ADMIN_IDS": "1",
        }
    )

    response = TestClient(create_app(settings, start_telegram=False)).get("/backup")

    assert response.status_code == 403