from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from urllib.parse import parse_qs

import httpx
import pytest

from vpn_control_plane.data import NodeRecord
from vpn_control_plane.xui import XuiAuthError, XuiNodeClient, XuiNodeEndpoint, decode_subscription_lines


def node(**overrides: object) -> NodeRecord:
    values: dict[str, object] = {
        "id": 1,
        "host": "panel.example.test",
        "port": 2053,
        "webBasePath": "secret-panel",
        "username": "admin",
        "password": "password",
        "scheme": "https",
    }
    values.update(overrides)
    return NodeRecord.model_validate(values)


def json_response(value: object, *, status_code: int = 200, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status_code, json=value, headers=headers)


def inbound_payload(
    inbound_id: int = 1,
    protocol: str = "vless",
    clients: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "id": inbound_id,
        "protocol": protocol,
        "settings": json.dumps({"clients": clients or []}),
        "streamSettings": json.dumps({"network": "tcp"}),
        "sniffing": "{}",
    }


@pytest.mark.asyncio
async def test_builds_node_base_url_with_normalized_web_base_path() -> None:
    endpoint = XuiNodeEndpoint.from_node(node(webBasePath="panel"))

    assert endpoint.base_url == "https://panel.example.test:2053/panel/"


@pytest.mark.asyncio
async def test_login_posts_credentials_and_two_factor_code() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return json_response({"success": True, "msg": "ok"}, headers={"set-cookie": "session=abc"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = XuiNodeClient(node(twoFactorCode="123456"), http_client=http_client)
        await client.login()

    form = parse_qs(requests[0].content.decode())
    assert str(requests[0].url) == "https://panel.example.test:2053/secret-panel/login/"
    assert form == {"username": ["admin"], "password": ["password"], "twoFactorCode": ["123456"]}


@pytest.mark.asyncio
async def test_login_failure_raises_auth_error_without_password() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response({"success": False, "msg": "bad credentials"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = XuiNodeClient(node(), http_client=http_client)
        with pytest.raises(XuiAuthError) as error:
            await client.login()

    assert "bad credentials" in str(error.value)
    assert "password" not in str(error.value)


@pytest.mark.asyncio
async def test_list_inbounds_logs_in_and_parses_json_string_fields() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/login/"):
            return json_response({"success": True})
        return json_response({"success": True, "obj": [inbound_payload(clients=[{"email": "1_123"}])]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        inbounds = await XuiNodeClient(node(), http_client=http_client).list_inbounds()

    assert paths == ["/secret-panel/login/", "/secret-panel/panel/api/inbounds/list"]
    assert inbounds[0].settings == {"clients": [{"email": "1_123"}]}
    assert inbounds[0].stream_settings == {"network": "tcp"}


@pytest.mark.asyncio
async def test_relogs_in_and_retries_once_for_expired_session() -> None:
    responses: Iterator[httpx.Response] = iter(
        [
            json_response({"success": True}),
            json_response({"success": False, "msg": "please login"}),
            json_response({"success": True}),
            json_response({"success": True, "obj": []}),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return next(responses)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        inbounds = await XuiNodeClient(node(), http_client=http_client).list_inbounds()

    assert inbounds == []


@pytest.mark.asyncio
async def test_get_inbound_returns_none_when_api_reports_missing() -> None:
    responses: Iterator[httpx.Response] = iter([json_response({"success": True}), json_response({"success": False})])

    def handler(request: httpx.Request) -> httpx.Response:
        return next(responses)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        inbound = await XuiNodeClient(node(), http_client=http_client).get_inbound(404)

    assert inbound is None


@pytest.mark.asyncio
async def test_add_client_treats_duplicate_email_as_idempotent_after_reread() -> None:
    existing = {"email": "1_123", "id": "existing-uuid", "subId": "legacy-sub"}
    responses: Iterator[httpx.Response] = iter(
        [
            json_response({"success": True}),
            json_response({"success": False, "msg": "Duplicate email"}),
            json_response({"success": True, "obj": inbound_payload(clients=[dict(existing)])}),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return next(responses)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        result = await XuiNodeClient(node(), http_client=http_client).add_client(1, {"email": "1_123"})

    assert result.created is False
    assert result.client == existing


@pytest.mark.asyncio
async def test_fetch_subscription_links_accepts_base64_and_plain_text() -> None:
    encoded = base64.b64encode(b"vless://one#One\ntrojan://two#Two\n").decode()

    assert decode_subscription_lines(encoded) == ["vless://one#One", "trojan://two#Two"]
    assert decode_subscription_lines("vless://plain#Plain\n") == ["vless://plain#Plain"]


@pytest.mark.asyncio
async def test_fetch_subscription_links_uses_node_subscription_base_url() -> None:
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(200, text="vless://plain#Plain\n")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        links = await XuiNodeClient(
            node(subscriptionBaseUrl="https://sub.example.test/sub/"),
            http_client=http_client,
        ).fetch_subscription_links("client 1")

    assert seen_urls == ["https://sub.example.test/sub/client%201"]
    assert links == ["vless://plain#Plain"]